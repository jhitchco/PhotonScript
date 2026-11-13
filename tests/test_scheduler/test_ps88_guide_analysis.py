"""PS-88: PHD2 guide-log analysis (why guiding failed, per night and per sub).

Fixtures are trimmed, verbatim slices of the scope PC's real guide logs
(tests/test_scheduler/fixtures/phd2): 2026-09-26 (every guided frame is
ErrorCode 1 STAR_SATURATED; East calibration used on the West pier; guide
pulses with no effect), 2026-09-25 (Guiding Assistant, guide output toggled,
a failed calibration where the star never moved, a static 'star') and
2026-09-18 (PHD2 profile at 600 mm on the 3248 mm OAG path)."""
import os
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import app
from photonscript.scheduler import phd2_analysis as pa
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler.routers import triage
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "phd2"
N26 = "PHD2_GuideLog_2026-09-26_120453.txt"
N25 = "PHD2_GuideLog_2026-09-25_192414.txt"
N18 = "PHD2_GuideLog_2026-09-18_204456.txt"


def _cfg(**kw):
    return PhotonScriptConfig(_env_file=None, **kw)


def _secs(name):
    return pl.parse_guide_log((FIX / name).read_text(encoding="utf-8"), name)


@pytest.fixture(scope="module")
def a26():
    return pa.analyze_sections(_secs(N26), _cfg(), date="2026-09-26")


@pytest.fixture(scope="module")
def a25():
    return pa.analyze_sections(_secs(N25), _cfg(), date="2026-09-25")


def _ids(findings):
    return [f["id"] for f in findings]


# --- parser ------------------------------------------------------------------

def test_find_result_codes_follow_phd2_star_h():
    assert pl.FIND_RESULT[0] == "STAR_OK" and pl.FIND_RESULT[1] == "STAR_SATURATED"
    assert pl.FIND_RESULT[2] == "STAR_LOWSNR" and pl.FIND_RESULT[7] == "STAR_MASSCHANGE"
    assert pl.FIND_RESULT[5] == "STAR_HIHFD" and pl.FIND_RESULT[8] == "STAR_ERROR"
    assert pl.GUIDED_CODES == (0, 1)


def test_saturated_frames_are_guided_and_drop_reasons_come_from_the_log():
    """The PS-73 bug: code 1 counted as a drop (0 frames, 4075 dropped)."""
    out = pl.summarize_sections(_secs(N26), _cfg())
    t = out["totals"]
    assert t["frames"] == 372 and t["saturated_frames"] == 372
    assert t["dropped_frames"] == 48
    assert out["star_lost_reasons"] == {"Star lost - mass changed": 38,
                                        "Star lost - low SNR": 6,
                                        "No star found": 4}
    assert t["rms_total_arcsec"] is not None


def test_drop_rows_keep_their_quoted_message_and_code():
    frames = [f for s in _secs(N26) if s["kind"] == "guiding" for f in s["frames"]]
    drops = [f for f in frames if f["drop"]]
    assert {f["code"] for f in drops} >= {2, 7}
    assert all(f["reason"] for f in drops)
    assert {f["code"] for f in drops if f["reason"] == "No star found"} <= {5, 8}
    assert not any(f["drop"] for f in frames if f["mount"] == "Mount")


def test_sections_split_on_begins_even_without_an_end_line():
    secs = _secs(N26)
    kinds = [(s["kind"], s["start"][11:], s["closed"]) for s in secs]
    # guiding at 21:30 is cut short by the calibration at 21:32 (no Ends line),
    # and that calibration is abandoned by a 'Guiding Ends'
    assert ("guiding", "21:30:08", "interrupted") in kinds
    assert ("calibration", "21:32:52", "aborted") in kinds


def test_settings_from_each_section_header():
    heart = next(s for s in _secs(N26) if s["start"].endswith("21:54:09"))
    h = heart["header"]
    assert (h["search_region_px"], h["mass_tolerance"], h["multi_star"]) == (15, "50.0%", True)
    assert (h["camera"], h["gain"], h["binning"]) == ("GP678C", 100, 2)
    assert (h["pixel_scale"], h["focal_length_mm"], h["exposure_ms"]) == (0.25, 3248, 5000)
    assert h["x_algorithm"] == "Lowpass2" and h["x_params"]["aggressiveness"] == 55
    assert h["y_params"] == {"aggressiveness": 50.0, "minimum move": 0.76}
    assert (h["backlash_comp"], h["max_ra_ms"], h["max_dec_ms"], h["dec_mode"]) == \
        ("disabled", 2500, 2500, "Auto")
    assert h["have_dark"] is False and h["defect_map"] is True   # "defect map in use"
    assert (h["x_angle"], h["parity"], h["ortho_err_deg"]) == (-93.4, "+/-", 15.1)
    assert (h["pier_side"], h["dec_deg"], h["norm_rate_ra"], h["norm_rate_dec"]) == \
        ("West", 61.6, 8.3, 8.0)
    assert h["cal_timestamp"] == "9/26/2026 21:44:20"


def test_ortho_error_matches_phd2():
    assert pl.ortho_error(86.6, 11.7) == 15.1
    assert pl.ortho_error(91.6, 3.4) == 1.8
    assert pl.ortho_error(100.2, 1.0) == 9.2
    assert pl.ortho_error(9.1, 14.3) == 84.8


# --- calibrations ------------------------------------------------------------

def test_calibration_records_and_quality(a26):
    by = {c["start_local"][11:16]: c for c in a26["calibrations"]}
    c = by["21:42"]
    assert (c["result"], c["pier_side"], c["dec_deg"], c["hour_angle_hr"]) == \
        ("complete", "East", 66.6, 2.86)
    assert (c["ra"]["angle_deg"], c["ra"]["rate_px_s"]) == (86.6, 13.0)
    assert (c["dec"]["angle_deg"], c["dec"]["rate_px_s"]) == (11.7, 31.353)
    assert c["ortho_err_deg"] == 15.1 and c["quality"] == "poor"
    assert c["steps"]["West"] == 5 and c["steps"]["North"] == 2
    assert any("Dec 66.6" in i for i in c["issues"])
    assert by["20:51"]["ortho_err_deg"] == 1.8
    assert by["20:17"]["ortho_err_deg"] == 9.2
    stuck = by["21:32"]
    assert stuck["result"] == "aborted" and stuck["steps"]["West"] == 43
    assert any("barely moved" in i for i in stuck["issues"])


def test_session_uses_the_timestamped_calibration_and_flags_the_pier(a26):
    heart = next(s for s in a26["sessions"] if s["start_local"].endswith("21:54:09"))
    cal = heart["calibration"]
    assert cal["start_local"].endswith("21:42:14") and cal["pier_side"] == "East"
    assert cal["pier_mismatch"] is True and cal["flipped_by_phd2"] is True
    assert cal["ortho_err_deg"] == 15.1
    assert "calibration_other_pier" in _ids(heart["findings"])


# --- guiding sessions --------------------------------------------------------

def test_times_are_local_in_the_log_and_utc_through_the_observatory_tz(a26):
    s1 = a26["sessions"][0]
    assert s1["start_local"] == "2026-09-26 20:18:08"
    assert s1["start_utc"] == "2026-09-27T02:18:08Z"          # MDT = UTC-6
    assert pa.to_utc(_cfg(), datetime(2026, 12, 1, 20, 0)) == \
        datetime(2026, 12, 2, 3, 0)                            # MST = UTC-7


def test_heart_session_pulses_did_not_move_the_mount(a26):
    heart = next(s for s in a26["sessions"] if s["start_local"].endswith("21:54:09"))
    ra = heart["corrections"]["ra"]
    assert ra["response_verdict"] == "not moving"
    assert ra["commanded_arcsec_min"] < -50 and abs(ra["observed_arcsec_min"]) < 5
    assert ra["dominant"] == "W" and ra["at_max_pct"] > 80
    assert heart["rms"]["all"]["ra_arcsec"] > 15
    top = a26["findings"][0]
    assert top["id"] == "pulses_not_moving" and top["severity"] == "critical"
    assert "commanded" in top["detail"] and "Manual Guide" in top["recommendation"]


def test_a_working_session_is_not_flagged(a26, a25):
    good = a26["sessions"][0]              # 20:18, Dec 0, fresh calibration
    assert good["rms"]["all"]["total_arcsec"] < 1.0
    assert "pulses_not_moving" not in _ids(good["findings"])
    real = a25["sessions"][-1]             # 01:36, a real star, 0.45" RMS
    assert real["epochs"]["pooled_std_arcsec"] > 0.3
    assert not {"pulses_not_moving", "star_static"} & set(_ids(real["findings"]))


def test_dithers_against_the_search_region_and_settling(a26):
    t = a26["totals"]
    assert t["dithers"] == 2 and t["max_dither_px"] == pytest.approx(20.1, abs=0.05)
    first = a26["sessions"][0]["dithers"]
    assert first["each"][0]["size_px"] == pytest.approx(20.1, abs=0.05)
    assert first["each"][0]["recovered"] is True
    assert t["settle"]["completed"] + t["settle"]["failed"] >= 3
    assert "search_region_small" in _ids(a26["findings"])


def test_saturation_mass_change_and_oversampling_rules(a26):
    ids = _ids(a26["findings"])
    for rule in ("saturated_star", "mass_change_drops", "oversampled", "calibration_ortho"):
        assert rule in ids
    sat = next(f for f in a26["findings"] if f["id"] == "saturated_star")
    assert "gain 100" in sat["detail"]
    # 'saturated' at SNR ~20 is the camera's ADU ceiling (8-bit mode), not the star
    assert sat["evidence"]["adu_limited"] is True
    assert "8-bit" in sat["detail"] and "16-bit" in sat["recommendation"]


def test_guiding_assistant_and_guide_output_off(a25):
    ga = a25["guiding_assistant"]["this_night"]
    assert len(ga) == 1
    g = ga[0]
    assert (g["ra_drift_arcsec_min"], g["dec_drift_arcsec_min"]) == (-1.5, -0.07)
    assert (g["backlash_ms"], g["pa_error_arcmin"]) == (0.0, 0.3)
    assert len(g["recommendations"]) == 4
    frames = [f for s in _secs(N25) if s["kind"] == "guiding" for f in s["frames"]]
    assert any(not f["output"] for f in frames)   # MountGuidingEnabled = false


def test_calibration_that_never_moved_and_a_static_star(a25):
    ids = _ids(a25["findings"])
    assert "calibration_no_motion" in ids and "star_static" in ids
    cal = next(c for c in a25["calibrations"] if c["result"] == "failed")
    assert cal["steps"]["West"] == 61 and cal["moved_px"]["West"] < 3
    static = next(s for s in a25["sessions"] if s["start_local"].endswith("22:15:42"))
    assert static["epochs"]["static_minutes"] >= 5
    assert static["duration_min"] < 10          # ends at its last frame, not 01:36


def test_older_night_profile_scale_and_one_way_dec():
    a = pa.analyze_sections(_secs(N18), _cfg(), date="2026-09-18")
    ids = _ids(a["findings"])
    assert "scale_mismatch" in ids and "dec_one_direction" in ids
    f = next(f for f in a["findings"] if f["id"] == "scale_mismatch")
    assert "600 mm" in f["detail"]
    assert "Star lost - low mass" in a["totals"]["drop_reasons"]


# --- per sub -----------------------------------------------------------------

def test_sub_start_from_file_name_date_obs_or_end_time():
    c = _cfg()
    assert pa.sub_start_utc({"file": "LIGHT\\2026-09-26_22-26-32__O_300.00s_0005.fits"}, c) == \
        datetime(2026, 9, 27, 4, 26, 32)
    assert pa.sub_start_utc({"file": "x.fits", "date_obs": "2026-09-27T04:26:32.5"}, c) == \
        datetime(2026, 9, 27, 4, 26, 32, 500000)
    assert pa.sub_start_utc({"file": "x.fits", "time": "2026-09-27T04:31:32Z",
                             "exp_s": 300}, c) == datetime(2026, 9, 27, 4, 26, 32)


def test_per_sub_guiding_during_the_exposure():
    secs = _secs(N26)
    subs = [{"rig": "rc16", "file": "LIGHT\\2026-09-26_22-05-22__O_300.00s_0001.fits",
             "exp_s": 300.0, "passed_qa": False, "target": "Heart Nebula",
             "filter": "OIII"},
            {"rig": "rc16", "file": "LIGHT\\2026-09-26_20-18-28__H_60.00s_0002.fits",
             "exp_s": 60.0, "passed_qa": True, "filter": "Ha"},
            {"rig": "rc16", "file": "LIGHT\\2026-09-26_19-00-00__H_60.00s_0000.fits",
             "exp_s": 60.0, "passed_qa": True}]
    a = pa.analyze_sections(secs, _cfg(), date="2026-09-26", subs=subs)
    rows = {r["file"][6:25]: r for r in a["per_sub"]["subs"]}
    heart, good, early = rows["2026-09-26_22-05-22"], rows["2026-09-26_20-18-28"], \
        rows["2026-09-26_19-00-00"]
    assert heart["state"] == "guided" and heart["frames"] > 30
    assert heart["rms_total_arcsec"] > 10 and heart["max_pulses"] > 20
    assert good["rms_total_arcsec"] < 1.0 and good["std_total_arcsec"] < 1.0
    assert early["state"] == "unguided" and early["rms_total_arcsec"] is None
    s = a["per_sub"]["summary"]
    assert (s["subs"], s["unguided"]) == (3, 1)
    assert s["median_rms_accepted_arcsec"] < s["median_rms_rejected_arcsec"]
    # star loss / SNR by the RC16 filter the OAG looks through
    assert set(s["rc16_by_filter"]) == {"Ha", "OIII"}
    assert s["rc16_by_filter"]["OIII"]["median_snr"] is not None


def test_guide_timeline_helper_for_ps21():
    secs = _secs(N26)
    raw = [(s, pl._scale_for(s["header"], None)[0]) for s in secs
           if s["kind"] == "guiding" and s["frames"]]
    tl = pa.GuideTimeline(raw, _cfg())
    g = tl.stats(datetime(2026, 9, 27, 2, 18, 28), 60)
    assert g["state"] == "guided" and 0 < g["rms_total_arcsec"] < 1.0
    assert tl.stats(datetime(2026, 9, 27, 1, 0, 0), 60)["state"] == "unguided"


# --- endpoint, night report, CLI ---------------------------------------------

@pytest.fixture
def phd2_night(tmp_path, monkeypatch):
    d = tmp_path / "phd2cfg"
    d.mkdir()
    for name, mt in ((N25, datetime(2026, 9, 26, 6, 55)), (N26, datetime(2026, 9, 27, 6, 18))):
        p = d / name
        p.write_text((FIX / name).read_text(encoding="utf-8"), encoding="utf-8")
        os.utime(p, (mt.timestamp(), mt.timestamp()))
    monkeypatch.setattr(pl, "_users_roots", list)
    monkeypatch.setattr(pl.Path, "home", classmethod(lambda cls: tmp_path / "nohome"))
    cfg = _cfg(phd2_logs_dir=str(d), data_dir=str(tmp_path / "data"),
               nina_base_url="http://localhost:1888/v2/api")
    monkeypatch.setattr(app, "_config", cfg)
    pa._CACHE.clear()
    pa._RESULTS.clear()
    pa._GA_CACHE.clear()
    return cfg


def test_analysis_endpoint_reads_the_night_and_older_ga(phd2_night):
    from photonscript.scheduler.runs import append_sub_record
    append_sub_record(phd2_night, "2026-09-26", {
        "rig": "rc16", "file": "LIGHT\\2026-09-26_22-05-22__O_300.00s_0001.fits",
        "exp_s": 300.0, "passed_qa": False, "time": "2026-09-27T04:10:31Z"})
    a = triage.api_phd2_analysis(date="2026-09-26")
    assert a["ok"] and [f["file"] for f in a["files"]] == [N26]
    assert a["totals"]["guided_frames"] == 372
    # no GA that night: the 09-25 log's GA is the drift yardstick
    ref = a["guiding_assistant"]["reference"]
    assert ref and ref["ra_drift_arcsec_min"] == -1.5
    top = a["findings"][0]
    assert top["id"] == "pulses_not_moving" and "Guiding Assistant" in top["detail"]
    assert a["per_sub"]["summary"]["subs"] == 1
    assert triage.api_phd2_analysis(date="2026-09-20")["ok"] is False
    assert "per_sub" not in triage.api_phd2_analysis(date="2026-09-26", subs=False)


def test_night_report_gets_a_compact_guiding_block(phd2_night, monkeypatch):
    from photonscript.scheduler import runs
    monkeypatch.setattr(runs, "night_detail",
                        lambda config, date, backfill=True: {"date": date, "table": [],
                                                             "subs": []})
    monkeypatch.setattr(app, "_syncthing_pending_names", lambda: set())
    d = app.api_run_detail("2026-09-26", backfill=False)
    g = d["guiding"]
    assert g["ok"] and g["sessions"] == 4 and g["top_findings"]
    assert g["top_findings"][0]["title"] == "Guide pulses did not move the mount"
    assert g["detail_url"] == "/api/phd2/analysis?date=2026-09-26"
    assert app.api_run_detail("2026-09-10", backfill=False)["guiding"]["ok"] is False


def test_cli_guiding_report(monkeypatch, a26):
    from typer.testing import CliRunner

    from photonscript import cli
    seen = {}

    def fake(config, date="", file="", with_subs=True):
        seen.update(date=date, subs=with_subs)
        return dict(a26, files=[{"file": N26}])
    monkeypatch.setattr(pa, "night_analysis", fake)
    r = CliRunner().invoke(cli.app, ["guiding-report", "--date", "2026-09-26", "--no-subs"])
    assert r.exit_code == 0, r.output
    assert seen == {"date": "2026-09-26", "subs": False}
    assert "FINDINGS" in r.output and "Guide pulses did not move the mount" in r.output
    assert "CALIBRATIONS" in r.output and "SESSIONS" in r.output


def test_report_text_has_no_em_dashes(a26, a25):
    for a in (a26, a25):
        assert chr(0x2014) not in pa.format_report(dict(a, files=[]))
