"""drift-survey (telescope_agent/drift_survey.py, docs/DRIFT_SURVEY.md):
the drift fit, smear / sub-length math, the plan and timing, the live run
driven with fakes (tpoint-autocal's preflight, filter, camera hand-off and
solvers reused), the CLI flags / dry run and the remote allowlist. Fakes
only; nothing opens a socket, and no script sent ever syncs, parks or
touches TPoint."""
import json
import math
import random
import re
from pathlib import Path

import pytest

from photonscript.scheduler import remote_cli as rc
from photonscript.telescope_agent import drift_survey as ds
from photonscript.telescope_agent import tpoint_autocal as ta
from photonscript.telescope_agent import tpoint_autocal_live as tal

from tests.test_scheduler.test_tpoint_autocal_loop import (
    Clock, FakeNina, FakeSched, _apps, _cfg)

ROOT = Path(__file__).resolve().parents[2]


# ------------------------------------------------------------- the fit

def _solves(ra_rate, dec_rate, dec=40.0, ra0=5.0, times=(0, 60, 120, 180),
            noise=0.0, seed=1):
    """Synthetic J2000 solves drifting ra_rate (RA x cos Dec, "/min, + E)
    and dec_rate ("/min, + N), with Gaussian noise (arcsec) per axis."""
    rnd = random.Random(seed)
    cosd = math.cos(math.radians(dec))
    out = []
    for t in times:
        x = ra_rate * t / 60.0 + rnd.gauss(0, noise)
        y = dec_rate * t / 60.0 + rnd.gauss(0, noise)
        out.append({"t": t + 1000.0, "ra_h": ra0 + x / (15.0 * 3600.0 * cosd),
                    "dec_d": dec + y / 3600.0})
    return out


def test_fit_recovers_an_exact_drift():
    f = ds.fit_drift(_solves(0.8, -0.6))
    assert f["n"] == 4 and f["span_s"] == 180.0
    assert f["ra_rate"] == pytest.approx(0.8, abs=2e-3)
    assert f["dec_rate"] == pytest.approx(-0.6, abs=2e-3)
    assert f["rate"] == pytest.approx(1.0, abs=2e-3)
    # east and south: PA between 90 (E) and 180 (S)
    assert f["pa_deg"] == pytest.approx(math.degrees(math.atan2(0.8, -0.6)) % 360, abs=0.2)
    assert f["rms_arcsec"] < 0.01 and f["ra_rate_err"] < 0.01


@pytest.mark.parametrize("seed", range(8))
def test_fit_recovers_a_noisy_drift_within_tolerance(seed):
    # 0.25" per-axis solve noise, 4 solves over 180 s: slope sigma ~0.11"/min
    f = ds.fit_drift(_solves(1.0, 0.3, noise=0.25, seed=seed))
    assert f["ra_rate"] == pytest.approx(1.0, abs=0.4)
    assert f["dec_rate"] == pytest.approx(0.3, abs=0.4)
    assert f["rms_arcsec"] < 1.0
    assert f["ra_rate_err"] is not None and f["ra_rate_err"] < 0.3


def test_fit_handles_ra_wrap_high_dec_and_order():
    s = _solves(0.5, 0.0, dec=75.0, ra0=23.99995)
    assert any(x["ra_h"] >= 24.0 for x in s)
    for x in s:
        x["ra_h"] %= 24.0                      # wraps through 0 h
    f = ds.fit_drift(list(reversed(s)))        # any order
    assert f["ra_rate"] == pytest.approx(0.5, abs=5e-3)
    assert f["dec_rate"] == pytest.approx(0.0, abs=5e-3)
    assert f["pa_deg"] == pytest.approx(90.0, abs=1.0)


def test_fit_edge_cases():
    assert ds.fit_drift([]) is None
    assert ds.fit_drift(_solves(1, 1, times=(0,))) is None
    assert ds.fit_drift(_solves(1, 1, times=(0, 0))) is None
    two = ds.fit_drift(_solves(0.0, 1.2, times=(0, 120)))
    assert two["n"] == 2 and two["rms_arcsec"] == 0 and two["ra_rate_err"] is None
    assert two["pa_deg"] == pytest.approx(0.0, abs=0.1)        # due north
    still = ds.fit_drift(_solves(0.0, 0.0))
    assert still["rate"] == 0 and still["pa_deg"] is None
    # unsolved entries are skipped
    s = _solves(1, 0) + [{"t": 5000, "ra_h": None, "dec_d": None}]
    assert ds.fit_drift(s)["n"] == 4


# ------------------------------------------------------------- smear, sub length

def test_smear_and_sub_length():
    assert ds.smear_arcsec(1.0, 600) == pytest.approx(10.0)
    assert ds.smear_arcsec(-0.5, 300) == pytest.approx(2.5)
    assert ds.max_sub_s(1.0, 2.0) == pytest.approx(120.0)
    assert ds.max_sub_s(0.1, 2.0) == pytest.approx(1200.0)
    assert ds.max_sub_s(0.0, 2.0) is None
    assert ds.standard_sub(130) == 120 and ds.standard_sub(1200) == 1200
    assert ds.standard_sub(599.9999999) == 600
    assert ds.standard_sub(None) == ds.STANDARD_SUBS[-1]
    assert ds.standard_sub(30) is None
    sm = ds.point_smear({"rate": 0.2}, 2.0, 3.5)
    assert sm["smear"] == {300: 1.0, 600: 2.0, 900: 3.0, 1200: 4.0}
    assert sm["max_sub_s"] == 600 and sm["rec_sub_s"] == 600
    assert sm["ecc"][300] < sm["ecc"][600] < sm["ecc"][1200] < 1


def test_predicted_ecc_matches_the_2026_10_28_subs():
    # NGC 2403 unguided at ~3.5" FWHM: trail 2.8 / 6.1 / 10.2", ecc 0.49 / 0.78 / 0.86
    for trail, ecc in ((2.8, 0.49), (6.1, 0.78), (10.2, 0.86)):
        assert ds.predicted_ecc(trail, 3.5) == pytest.approx(ecc, abs=0.04)
    assert ds.predicted_ecc(0.0, 3.5) == 0.0
    assert ds.predicted_ecc(2.0, 3.5) == pytest.approx(0.36, abs=0.01)
    for e in (0.3, 0.5, 0.7):
        assert ds.predicted_ecc(ds.smear_for_ecc(e, 3.5), 3.5) == pytest.approx(e, abs=1e-6)


# ------------------------------------------------------------- the plan

_PTS = [{"alt": 45.0, "az": 100.0, "ha_h": -2.0, "dec_deg": 10.0, "side": "east"},
        {"alt": 62.0, "az": 60.0, "ha_h": -1.5, "dec_deg": 40.0, "side": "east"},
        {"alt": 50.0, "az": 280.0, "ha_h": 2.5, "dec_deg": 30.0, "side": "west"}]


def test_frame_offsets_and_timing():
    assert ds.frame_offsets(180, 60) == [0, 60, 120, 180]
    assert ds.frame_offsets(100, 40) == [0, 40, 80, 100]
    assert ds.frame_offsets(60, 60) == [0, 60]
    with pytest.raises(ValueError):
        ds.frame_offsets(0, 60)
    assert ds.per_point_s(180, 60, 10, 10) == pytest.approx(
        ds.SLEW_EST_S + 10 + 180 + 10 + ds.SOLVE_EST_S)
    plan = ds.build_plan(_PTS, label="before")
    assert plan["frames_per_point"] == 4 and plan["flips"] == 1
    assert plan["est_s"] == round(3 * plan["per_point_s"] + tal.FLIP_S)
    p = plan["params"]
    assert (p["window_s"], p["every_s"], p["exposure_s"], p["settle_s"], p["binning"],
            p["solver"], p["filter"], p["max_smear_arcsec"], p["fwhm_arcsec"]) == \
        (180.0, 60.0, 10.0, 10.0, 1, "fallback", "L", 2.0, 3.5)
    assert p["image_scale"] == 0.236 and p["ecc_at_max_smear"] == 0.36
    assert ds.build_plan(_PTS, stop_nina2=True)["params"]["unpark"] is True


def test_validate_options():
    assert ds.validate(180, 60, 10, 10, 10) == []
    assert ds.validate(180, 20, 10, 10, 10)        # no time to download + solve
    assert ds.validate(180, 200, 10, 10, 10)       # every > window
    assert ds.validate(10, 10, 1, 0, 10)           # window too short
    assert ds.validate(180, 60, 10, 10, 0) and ds.validate(180, 60, 10, 10, 61)
    assert ds.validate(180, 60, 10, 999, 10)


def test_dry_run_text_lists_timing_scripts_and_no_tpoint():
    plan = ds.build_plan(_PTS, track=True)
    text = ds.format_plan(plan, None, {"model_date": "2026-10-28", "points": 83,
                                       "sky_rms_arcsec": 22.77})
    assert "about" in text and "+0, +60, +120, +180" in text
    assert "TPoint model: 2026-10-28, 83 points, sky RMS 22.8\"" in text
    assert "SlewToAzAlt" in text and "SetTracking" in text and "TakeImage" in text
    assert "Never syncs, never parks, never writes TPoint" in text
    js = [ln for ln in text.splitlines() if ln.strip().startswith("JS:")]
    assert len(js) >= 7
    for line in js:
        for pat, what in ds.NEVER:
            assert not re.search(pat, line), (what, line)


def test_select_uses_a_whole_window_as_pace_and_alt_30(tmp_path):
    cfg = _cfg(tmp_path, tpoint_mapping_min_alt=20.0)
    from datetime import datetime
    sel = ds.select(cfg, 10, "both", datetime(2026, 10, 29, 6, 0), 180, 60, 10, 10,
                    moon_altaz=[])
    assert sel["min_alt"] == 30.0                 # never under 30 here
    assert sel["pace_s"] == pytest.approx(ds.per_point_s(180, 60, 10, 10))
    assert sel["points"] and all(p["alt"] >= 30.0 for p in sel["points"])
    assert {p["side"] for p in sel["points"]} == {"east", "west"}


# ------------------------------------------------------------- summary

def _res(point, side, alt, rate, ra=None, dec=0.0):
    ra = rate if ra is None else ra
    return {"point": point, "side": side, "alt": alt, "az": 0.0,
            "fit": {"rate": rate, "ra_rate": ra, "dec_rate": dec,
                    "pa_deg": ds.position_angle(ra, dec)}}


def test_summary_by_side_region_and_recommendation():
    rs = [_res(1, "east", 40, 0.5), _res(2, "east", 70, 1.0), _res(3, "west", 50, 0.2),
          {"point": 4, "side": "west", "alt": 50, "fit": None}]
    s = ds.summarize(rs, max_smear=2.0, fwhm=3.5)
    assert s["measured"] == 3 and s["worst_rate"] == 1.0 and s["median_rate"] == 0.5
    assert s["worst_point"] == 2
    assert s["recommended_sub_s"] == 120          # 2" / 1"/min = 120 s
    assert s["typical_sub_s"] == 240              # 2" / 0.5 = 240 s
    assert s["smear_at_worst"][600] == 10.0
    assert set(s["by_side"]) == {"east", "west"}
    assert s["by_side"]["west"]["rec_sub_s_worst"] == 600
    assert set(s["by_region"]) == {"east alt 30-45", "east alt 60-90", "west alt 45-60"}
    assert s["mean_pa_deg"] == pytest.approx(90.0) and s["pa_agreement"] == 1.0
    text = "\n".join(ds.format_summary(s, {"model_date": "2026-10-28", "points": 83,
                                           "sky_rms_arcsec": 22.8}, "before"))
    assert "RECOMMENDED unguided sub: 120 s" in text and "(before)" in text
    assert "east alt 60-90" in text and "83 points" in text
    assert ds.summarize([])["measured"] == 0


def test_region_bands():
    assert ds.region_of(30, "east") == "east alt 30-45"
    assert ds.region_of(59.9, "west") == "west alt 45-60"
    assert ds.region_of(85, "west") == "west alt 60-90"
    assert ds.region_of(90, "west") == "west alt 60-90"


# ------------------------------------------------------------- the live run

RATES = {1: (0.8, -0.6), 2: (1.1, 0.2), 3: (0.3, 0.1)}     # ("/min E, "/min N)


class DriftSky:
    """TheSky for the drift run: the mount drifts RATES[point] from the
    slew, ImageLink returns the position at each exposure's midpoint (plus
    noise), the exposure takes `exposure` s of the shared clock."""

    def __init__(self, clock, rates=RATES, exposure=10.0, noise=0.1, tracking="1",
                 solve=lambda point, frame: True, seed=3):
        self.clock = clock
        self.rates = rates
        self.exposure = exposure
        self.noise = noise
        self.tracking = tracking
        self.solve = solve
        self.rnd = random.Random(seed)
        self.calls: list[str] = []
        self.scripts: list[str] = []
        self.point = 0
        self.frame = 0
        self.t_slew = 0.0
        self.mid = None

    def _pos(self, t):
        ra_r, dec_r = self.rates.get(self.point, (0.0, 0.0))
        dec0 = 20.0 + self.point
        m = (t - self.t_slew) / 60.0
        x = ra_r * m + self.rnd.gauss(0, self.noise)
        y = dec_r * m + self.rnd.gauss(0, self.noise)
        return (6.0 + self.point + x / (15 * 3600 * math.cos(math.radians(dec0))),
                dec0 + y / 3600.0)

    def run(self, js):
        self.scripts.append(js)

        def k(name):
            self.calls.append(name)
        if "SlewToAzAlt" in js:
            k("slew")
            self.point += 1
            self.frame = 0
            self.t_slew = self.clock.t
            return {"err": None}
        if "IsSlewComplete" in js:
            k("slew_poll")
            return {"slew_complete": "1"}
        if "SetTracking" in js:
            k("tracking_on")
            self.tracking = "1"
            return {"tracking": "1"}
        if "ComputeLocalSiderealTime" in js:
            k("mount_snapshot")
            ra, dec = self._pos(self.clock.t)
            return {"connected": "1", "ra_h": str(ra), "dec_d": str(dec), "az": "100",
                    "alt": "45", "tracking": self.tracking, "lst_h": "3.0",
                    "jd": "2461342.7"}
        if "TakeImage()" in js:
            k("expose")
            self.frame += 1
            self.mid = self.clock.t + self.exposure / 2.0
            return {"err": None}
        if "IsExposureComplete" in js:
            k("expose_poll")
            self.clock.sleep(self.exposure)
            return {"complete": "1", "last_image": rf"C:\img\p{self.point}f{self.frame}.fit"}
        if "ImageLink.execute()" in js:
            k("imagelink")
            self.clock.sleep(5.0)
            if not self.solve(self.point, self.frame):
                return {"exec_error": None, "succeeded": "0", "error_code": "212",
                        "error_text": "Process aborted. Error _ 212."}
            ra, dec = self._pos(self.mid)
            return {"exec_error": None, "succeeded": "1", "error_code": "0",
                    "ra_j2000_h": f"{ra:.9f}", "dec_j2000_d": f"{dec:.9f}",
                    "image_scale": "0.236", "solution_rms": "0.3", "image_stars": "120"}
        if "IsParked()" in js:
            k("park_read")
            return {"connected": "true", "parked": "false"}
        if "Abort()" in js:
            k("abort")
            return {"cam_err": "", "mount_err": ""}
        if "ccdsoftCamera.Connect()" in js:
            k("camera_connect")
        elif "ccdsoftCamera.Disconnect()" in js:
            k("camera_disconnect")
        elif "cameraModel" in js:
            k("camera_read")
            return {"camera_model": "ASCOM Camera"}
        elif "fovsToSearch" in js:
            k("ails_read")
            return {"exposure_s": "10", "image_scale": "0.236"}
        elif "ccdsoftCamera.ExposureTime =" in js:
            k("camera_setup")
        else:
            raise AssertionError(f"unexpected script {js[:80]}")
        return {"err": None, "status": "Ready"}


class Nina2:
    def __init__(self):
        self.state = "RUNNING"
        self.calls = []

    def sequence_state(self):
        return {"State": self.state}

    def stop_sequence(self):
        self.calls.append("stop")
        self.state = "IDLE"


def _no_tpoint(*a, **k):
    raise AssertionError("TPoint's files were touched")


def _survey(tmp_path, pts=_PTS, sky_kw=None, sched=None, opts_kw=None, dopts_kw=None,
            nina=None, nina2=None, astap=None, ask=lambda p: "yes"):
    clock = Clock()
    sky = DriftSky(clock, **(sky_kw or {}))
    nina = nina or FakeNina(clock)
    out: list[str] = []
    deps = tal.Deps(sky=sky, nina=nina, sched=sched or FakeSched(),
                    count=_no_tpoint, list_files=_no_tpoint, ask=ask,
                    say=out.append, sleep=clock.sleep, mono=clock.mono, now=clock.now,
                    copy=_no_tpoint, notify=lambda m: None, astap=astap,
                    import_ref=_no_tpoint, nina2=nina2)
    opts = tal.Opts(backup_root=str(tmp_path / "bk"), solver="fallback",
                    **(opts_kw or {}))
    kw = dict(stem=str(tmp_path / "runs" / "2026-10-28_drift_survey_0500Z_before"),
              label="before", model=lambda: {"model_date": "2026-10-28", "points": 83,
                                             "sky_rms_arcsec": 22.8})
    kw.update(dopts_kw or {})
    res = ds.run_survey(_cfg(tmp_path), pts, deps, opts, ds.DriftOpts(**kw))
    return res, sky, nina, "\n".join(out), clock


def _never_sent(sky):
    for js in sky.scripts:
        for pat, what in ds.NEVER:
            assert not re.search(pat, js), (what, js[:120])
        assert "AutoMap" not in js and "TPoint" not in js


def test_live_run_recovers_each_points_drift(tmp_path):
    res, sky, nina, text, clock = _survey(tmp_path)
    assert res["status"] == "done", res["reason"]
    assert res["measured"] == 3 and res["solves"] == 12 and res["frames"] == 12
    for r in res["points"]:
        want = RATES[r["point"]]
        assert r["fit"]["ra_rate"] == pytest.approx(want[0], abs=0.15), r
        assert r["fit"]["dec_rate"] == pytest.approx(want[1], abs=0.15), r
        assert r["fit"]["n"] == 4
    # the camera was lent and given back; NINA #1 only lent / took back
    assert res["camera_returned"] is True and nina.calls == ["disconnect", "connect"]
    assert sky.calls.count("slew") == 3 and sky.calls.count("expose") == 12
    # between a point's first and last frame: no slew, no sync, nothing moved
    for seg in "".join("S" if c == "slew" else "E" if c == "expose" else ""
                       for c in sky.calls).split("S")[1:]:
        assert seg == "EEEE"
    _never_sent(sky)
    assert "abort" not in sky.calls
    assert res["summary"]["worst_point"] == 2
    assert "RECOMMENDED unguided sub" in text and "Nothing was synced" in text


def test_frames_are_spaced_by_every_s_and_midpoints_fit(tmp_path):
    res, sky, *_ = _survey(tmp_path, pts=_PTS[:1])
    rows = list(_csv(tmp_path / "runs" / "2026-10-28_drift_survey_0500Z_before.csv"))
    t = [float(r["t_mid_s"]) for r in rows]
    assert t == pytest.approx([0, 60, 120, 180], abs=0.01)
    assert rows[0]["dx_arcsec"] == "0.0" and rows[-1]["frame"] == "4"
    assert all(r["solved_by"] == "imagelink" and r["label"] == "before" for r in rows)


def _csv(p):
    import csv
    with open(p, newline="", encoding="utf-8") as fh:
        yield from csv.DictReader(fh)


def test_files_csv_fits_and_json(tmp_path):
    res, *_ = _survey(tmp_path)
    stem = tmp_path / "runs" / "2026-10-28_drift_survey_0500Z_before"
    solves = list(_csv(Path(str(stem) + ".csv")))
    fits = list(_csv(Path(str(stem) + "_fits.csv")))
    assert len(solves) == 12 and len(fits) == 3
    for col in ("ra_j2000_h", "dec_j2000_d", "mid_utc", "exp_start_utc", "dx_arcsec"):
        assert col in solves[0]
    for col in ("rate", "pa_deg", "rms_arcsec", "smear_600", "ecc_1200", "rec_sub_s",
                "region"):
        assert col in fits[0]
    doc = json.loads(Path(str(stem) + ".json").read_text(encoding="utf-8"))
    assert doc["kind"] == "drift_survey" and doc["label"] == "before"
    assert doc["status"] == "done" and doc["camera_returned"] is True
    assert doc["model"] == {"model_date": "2026-10-28", "points": 83, "sky_rms_arcsec": 22.8}
    assert len(doc["points"]) == 3 and doc["points"][0]["fit"]["rate"] > 0
    assert doc["summary"]["measured"] == 3 and doc["params"]["frames"] == [0, 60, 120, 180]
    assert res["json"].endswith("_before.json")


def test_run_stem(tmp_path):
    from datetime import datetime
    s = ds.run_stem(_cfg(tmp_path), datetime(2026, 10, 29, 4, 7), "after TPoint!")
    assert s.endswith("_drift_survey_0407Z_after-TPoint")
    assert Path(s).parent == tmp_path / "runs"
    assert ds.run_stem(_cfg(tmp_path), datetime(2026, 10, 29, 4, 7)).endswith("_0407Z")


def test_preflight_reused_and_never_backs_up(tmp_path):
    res, sky, nina, text, _ = _survey(tmp_path, sched=FakeSched(states=("ARMED",)))
    assert res["status"] == "refused" and "armer ARMED" in res["reason"]
    assert sky.calls == [] or set(sky.calls) <= {"park_read"}
    assert nina.calls == [] and res["camera_returned"] is None
    assert "TPoint backup" not in text            # no backup check, no copy


def test_nina2_running_needs_stop_nina2_and_then_unparks_on(tmp_path):
    class Busy2(FakeSched):
        def apps(self):
            a = _apps()
            a["sequence_running"][ta.NINA2_LABEL] = ["Smart Exposure"]
            return a
    res, sky, *_ = _survey(tmp_path, sched=Busy2(), nina2=Nina2())
    assert res["status"] == "refused" and "--stop-nina2" in res["reason"]

    class After(Busy2):
        k = 0                       # (FakeSched counts arm() reads in .n)

        def apps(self):
            self.k += 1
            return super().apps() if self.k == 1 else _apps()
    n2 = Nina2()
    res, sky, nina, text, _ = _survey(tmp_path, sched=After(), nina2=n2,
                                      opts_kw={"stop_nina2": True}, pts=_PTS[:1])
    assert res["status"] == "done", res["reason"]
    assert n2.calls == ["stop"] and res["nina2_stopped"] is True
    assert "implies --unpark" in text


def test_not_tracking_stops_unless_track(tmp_path):
    res, sky, nina, text, _ = _survey(tmp_path, sky_kw={"tracking": "0"})
    assert res["status"] == "stopped" and "--track" in res["reason"]
    assert res["camera_returned"] is True and "expose" not in sky.calls
    res, sky, *_ = _survey(tmp_path, sky_kw={"tracking": "0"}, opts_kw={"track": True},
                           pts=_PTS[:1])
    assert res["status"] == "done" and sky.calls.count("tracking_on") == 1
    _never_sent(sky)


def test_imagelink_failure_falls_back_to_astap(tmp_path):
    seen = []

    def astap(path, scale, hint):
        seen.append((path, scale, hint))
        return {"ra": 15.0 * (6.0 + 1), "dec": 21.0}
    res, sky, *_ = _survey(tmp_path, pts=_PTS[:1], astap=astap,
                           sky_kw={"solve": lambda p, f: f != 2})
    assert res["status"] == "done"
    assert len(seen) == 1 and seen[0][1] == pytest.approx(0.236)
    assert seen[0][2] is not None                 # hinted by the previous solve
    rows = list(_csv(tmp_path / "runs" / "2026-10-28_drift_survey_0500Z_before.csv"))
    assert [r["solved_by"] for r in rows] == ["imagelink", "astap", "imagelink", "imagelink"]


def test_two_unmeasured_points_in_a_row_stop(tmp_path):
    res, sky, nina, text, _ = _survey(tmp_path, sky_kw={"solve": lambda p, f: False})
    assert res["status"] == "stopped" and "fewer than 2 solves" in res["reason"]
    assert res["attempted"] == 2 and res["measured"] == 0
    assert res["camera_returned"] is True and "NOT measured" in text


def test_time_budget_stops_starting_points(tmp_path):
    res, sky, *_ = _survey(tmp_path, dopts_kw={"max_run_s": 400.0})
    assert res["status"] == "stopped" and "time budget" in res["reason"]
    assert res["attempted"] == 1 and res["camera_returned"] is True


def test_operator_no_sends_nothing(tmp_path):
    res, sky, nina, *_ = _survey(tmp_path, ask=lambda p: "no")
    assert res["status"] == "refused" and nina.calls == []
    assert set(sky.calls) <= {"park_read"}
    res, sky, nina, *_ = _survey(tmp_path, ask=lambda p: "no", pts=_PTS[:1],
                                 dopts_kw={"unattended": True})
    assert res["status"] == "done"


def test_ctrl_c_mid_window_hands_the_camera_back(tmp_path):
    class Stop(DriftSky):
        def run(self, js):
            if "TakeImage()" in js and self.frame == 2:
                raise KeyboardInterrupt
            return super().run(js)
    clock = Clock()
    sky = Stop(clock)
    nina = FakeNina(clock)
    deps = tal.Deps(sky=sky, nina=nina, sched=FakeSched(), count=_no_tpoint,
                    list_files=_no_tpoint, ask=lambda p: "yes", say=lambda m: None,
                    sleep=clock.sleep, mono=clock.mono, now=clock.now, copy=_no_tpoint)
    res = ds.run_survey(_cfg(tmp_path), _PTS, deps, tal.Opts(),
                        ds.DriftOpts(stem=str(tmp_path / "x")))
    assert res["status"] == "stopped" and res["reason"] == "operator Ctrl+C"
    assert res["camera_returned"] is True and nina.calls == ["disconnect", "connect"]
    # the solves before the stop were still written
    assert len(list(_csv(tmp_path / "x.csv"))) == 2


def test_the_guard_refuses_sync_park_and_tpoint_scripts(tmp_path):
    clock = Clock()
    deps = tal.Deps(sky=DriftSky(clock), nina=FakeNina(clock), sched=FakeSched(),
                    count=_no_tpoint, list_files=_no_tpoint, say=lambda m: None)
    run = ds.DriftRun(_cfg(tmp_path), _PTS, deps, tal.Opts(), ds.DriftOpts())
    for js in ("sky6RASCOMTele.Sync(1, 2, 'x');", "sky6RASCOMTele.Park();",
               "ClosedLoopSlew.exec();", "sky6RASCOMTheSky.AutoMap();",
               "TPoint.Save();", "sky6StarChart.SetDocumentProperty(3, 0);",
               "sky6RASCOMTele.DoCommand(1, 'ProTrack');"):
        with pytest.raises(tal.StopRun):
            run.sky("x", js)
    # the reviewed reads go through
    assert run.sky("park", ta.mount_park_state_script())["parked"] == "false"


# ------------------------------------------------------------- CLI, allowlist

def _cli(tmp_path, monkeypatch, args):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    monkeypatch.setattr(tal, "live_deps", lambda *a, **k: None)
    monkeypatch.setattr(tal, "dark_end", lambda c, n=None: None)
    monkeypatch.setattr(tal.tm, "moon_track", lambda *a, **k: [])
    monkeypatch.setattr(ds, "model_info", lambda c: {"model_date": "2026-10-28",
                                                     "points": 83, "sky_rms_arcsec": 22.8})
    seen = {}

    def fake_run(config, points, deps, opts, dopts):
        seen.update(points=points, opts=opts, dopts=dopts)
        return {"status": "done"}
    monkeypatch.setattr(ds, "run_survey", fake_run)
    r = CliRunner().invoke(cli.app, ["drift-survey"] + args)
    return r, seen


def test_cli_dry_run_is_the_default_and_sends_nothing(tmp_path, monkeypatch):
    r, seen = _cli(tmp_path, monkeypatch, ["--no-scripts"])
    assert r.exit_code == 0, r.output
    assert seen == {}
    flat = " ".join(r.output.split())           # the console wraps long lines
    assert "Drift survey DRY RUN" in flat and "+0, +60, +120, +180" in flat
    assert "83 points, sky RMS 22.8" in flat and "Points for --go" in flat
    r, seen = _cli(tmp_path, monkeypatch, ["--dry-run", "--json"])
    assert r.exit_code == 0 and json.loads(r.output)["plan"]["frames_per_point"] == 4


def test_cli_go_defaults_and_flags(tmp_path, monkeypatch):
    r, seen = _cli(tmp_path, monkeypatch, ["--go"])
    assert r.exit_code == 0, r.output
    o, d = seen["opts"], seen["dopts"]
    assert len(seen["points"]) == 10
    assert (o.exposure_s, o.binning, o.filter, o.solver, o.track, o.unpark,
            o.stop_nina2) == (10.0, 1, "L", "fallback", False, False, False)
    assert (d.window_s, d.every_s, d.settle_s, d.max_smear, d.fwhm, d.label,
            d.unattended, d.max_run_s) == (180.0, 60.0, 10.0, 2.0, 3.5, "", False, None)
    assert "_drift_survey_" in d.stem
    r, seen = _cli(tmp_path, monkeypatch, [
        "--go", "--points", "4", "--side", "west", "--window-s", "120", "--every-s",
        "40", "--exposure-s", "8", "--binning", "2", "--filter", "none", "--settle-s",
        "5", "--max-smear-arcsec", "2.8", "--fwhm-arcsec", "3.0", "--unpark", "--track",
        "--stop-nina2", "--solver", "astap", "--label", "after"])
    assert r.exit_code == 0, r.output
    o, d = seen["opts"], seen["dopts"]
    assert len(seen["points"]) <= 4 and {p["side"] for p in seen["points"]} == {"west"}
    assert (o.exposure_s, o.binning, o.filter, o.solver, o.track, o.unpark,
            o.stop_nina2) == (8.0, 2, None, "astap", True, True, True)
    assert (d.window_s, d.every_s, d.settle_s, d.max_smear, d.fwhm, d.label) == \
        (120.0, 40.0, 5.0, 2.8, 3.0, "after")
    assert d.stem.endswith("_after")


def test_cli_unattended_budget_and_bad_options(tmp_path, monkeypatch):
    # 10 points x ~270 s is ~46 min: over the 28 min unattended budget
    r, seen = _cli(tmp_path, monkeypatch, ["--go", "--unattended"])
    assert r.exit_code == 2 and seen == {} and "28 min budget" in r.output
    r, seen = _cli(tmp_path, monkeypatch, ["--go", "--unattended", "--points", "5"])
    assert r.exit_code == 0, r.output
    assert seen["dopts"].unattended is True and seen["dopts"].max_run_s == 28 * 60
    for bad in (["--every-s", "15"], ["--side", "north"], ["--solver", "x"],
                ["--points", "0"], ["--go", "--dry-run"], ["--binning", "5"]):
        r, seen = _cli(tmp_path, monkeypatch, bad)
        assert r.exit_code == 2 and seen == {}, bad


def test_remote_allowlist_entry():
    allow = rc.load_allowlist()
    cmd = allow.commands["drift-survey"]
    assert cmd.cls == "read" and cmd.write_flags == ("--go",)
    assert not cmd.require and not cmd.night_safe
    for f in ("--points", "--side", "--window-s", "--every-s", "--exposure-s",
              "--binning", "--filter", "--settle-s", "--max-smear-arcsec",
              "--fwhm-arcsec", "--dry-run", "--go", "--unattended", "--unpark",
              "--track", "--stop-nina2", "--solver", "--label"):
        assert f in cmd.flags, f
    assert rc.check_allowlist(allow) == []
    assert rc.validate(["drift-survey", "--dry-run", "--points", "6"], allow).cls == "read"
    assert rc.validate(["drift-survey"], allow).cls == "read"
    p = rc.validate(["drift-survey", "--go", "--unattended", "--points", "5",
                     "--label", "before", "--track", "--stop-nina2"], allow)
    assert p.cls == "write"
    for args in (["drift-survey", "--max-run-min", "60"],
                 ["drift-survey", "--url", "http://x"]):
        with pytest.raises(rc.Refused) as e:
            rc.validate(args, allow)
        assert e.value.status == 403


def test_sources_stay_ascii_without_em_dashes():
    for rel in ("photonscript/telescope_agent/drift_survey.py", "docs/DRIFT_SURVEY.md",
                "config/remote_cli.toml", "tests/test_scheduler/test_drift_survey.py"):
        src = (ROOT / rel).read_text(encoding="utf-8")
        assert "\u2014" not in src and "\u2013" not in src, rel
        assert src.isascii(), rel
