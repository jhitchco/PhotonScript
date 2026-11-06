"""Sky flats v2 (2026-10-24): windows by sun altitude, both twilights.

Pins: the per-rig / filter sky model fit on NINA SkyFlat log lines
(sky_flat_model.py), its refresh after a window and the change it reports,
the plan (flat_plan.py: dusk narrowband -> R/G/B -> L, dawn L -> B/G/R ->
narrowband, a late roof skips what has passed, the dawn hard end, the flat
spot opposite the sun), the owed carry-over between the two windows, the
truthful window report ("none saved (why)") and that the sequences built
from a plan lint clean.
"""
import asyncio
import calendar
import json
import math
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import dusk_flats as df
from photonscript.scheduler import dusk_flats_check as dk
from photonscript.scheduler import flat_plan as fp
from photonscript.scheduler import sky_flat_model as sfm
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.localtime import utc_offset_hours

NIGHT = "2026-10-23"         # sunset 18:35, naut dawn 06:31, sunrise 07:25 MDT
ALL7 = ["L", "R", "G", "B", "Ha", "OIII", "SII"]
_SKYFLAT = "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat"


def _cfg(tmp_path, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    kw.setdefault("evening_forecast_enabled", False)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              image_watch_dir=str(tmp_path / "nina"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


# ---- synthetic NINA SkyFlat lines ------------------------------------------------

TRUTH = {"slope": 0.95, "bias": 250.0, "filters": {"L": 11.9, "Ha": 8.3}}


def _lines(cfg, flt_raw, flt, t0_local, n=12, step_s=30, exp0=15.05, truth=TRUTH):
    """A NINA exposure search: SkyFlat start, the filter move, n probes."""
    out = [f"{t0_local:%Y-%m-%dT%H:%M:%S}.0000|INFO|SequenceItem.cs|Run|208|Starting "
           "Category: Flat Device, Container: NINA.Sequencer.SequenceItem.FlatDevice."
           "SkyFlat, Strategy: SequentialStrategy, Items[CREATED]: 2",
           f"{t0_local:%Y-%m-%dT%H:%M:%S}.1000|INFO|SkyFlat.cs|Execute|219|Determining "
           "Sky Flat Exposure Time. Min 1, Max 30, Target 50%, Tolerance 20%",
           f"{t0_local:%Y-%m-%dT%H:%M:%S}.2000|INFO|FilterWheelVM.cs|ChangeFilter|112|"
           f"Moving to Filter {flt_raw} at Position 6"]
    exp = exp0
    for i in range(n):
        t = t0_local + timedelta(seconds=step_s * (i + 1))
        utc = t - timedelta(hours=utc_offset_hours(cfg, t + timedelta(hours=6)))
        alt = sfm.sun_alt(cfg, utc)
        adu = truth["bias"] + exp * math.exp(truth["filters"][flt] + truth["slope"] * alt)
        kind = "dim" if adu < 26000 else "bright"
        word = "higher" if kind == "dim" else "lower"
        out.append(f"{t:%Y-%m-%dT%H:%M:%S}.5000|INFO|SkyFlat.cs|DetermineExposureTime|331|"
                   f"Exposure too {kind} at {exp}s. ADU measured at: {adu}. Retrying "
                   f"with {word} exposure time")
        exp = min(30.0, exp * 1.2)
    return out


def test_parse_reads_probes_filter_sun_and_phase(tmp_path):
    cfg = _cfg(tmp_path)
    lines = _lines(cfg, "H", "Ha", datetime(2026, 10, 24, 7, 0))
    ps = sfm.parse_lines(cfg, lines, "rc16")
    assert len(ps) == 12
    assert {p["filter"] for p in ps} == {"Ha"}          # H -> Ha (nina_filter_names)
    assert {p["phase"] for p in ps} == {"dawn"}
    assert ps[0]["kind"] == "dim" and ps[0]["exp"] == 15.05
    # the sun climbs through the search
    assert ps[-1]["alt"] > ps[0]["alt"]
    # the 10-23 log line with a trailing period parses
    one = sfm.parse_lines(cfg, [
        "2026-10-23T18:55:00.0|INFO|SequenceItem.cs|Run|208|Starting Category: Flat "
        "Device, Container: NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat",
        "2026-10-23T18:55:32.3072|INFO|SkyFlat.cs|DetermineExposureTime|331|Exposure "
        "too dim at 15.05s. ADU measured at: 889.100856761826. Retrying with higher "
        "exposure time",
        "2026-09-08T06:53:24.6259|INFO|SkyFlat.cs|GetNextExposureTimeByADU|763|Adding "
        "ADU=33541.942830042455, Exposure Time=8.263902259049642",
        "2026-09-08T06:53:15.4230|INFO|SkyFlat.cs|DetermineExposureTime|319|Found "
        "exposure time at 8.667202461145479s with histogram ADU 34366.64032837255"],
        "piggyback")
    assert [(p["kind"], p["filter"], p["phase"]) for p in one] == [
        ("dim", "OSC", "dusk"), ("flat", "OSC", "dawn"), ("found", "OSC", "dawn")]
    assert one[1]["exp"] == pytest.approx(8.2639, abs=1e-3)


def test_fit_recovers_slope_and_intercepts_from_synthetic_logs(tmp_path):
    cfg = _cfg(tmp_path)
    lines = (_lines(cfg, "L", "L", datetime(2026, 10, 24, 6, 50), n=20)
             + _lines(cfg, "H", "Ha", datetime(2026, 10, 24, 7, 12), n=20))
    ps = sfm.parse_lines(cfg, lines, "rc16")
    f = sfm.fit_rig(ps, "rc16")
    assert f["slope"] == pytest.approx(TRUTH["slope"], abs=0.02)
    assert f["filters"]["L"] == pytest.approx(TRUTH["filters"]["L"], abs=0.05)
    assert f["filters"]["Ha"] == pytest.approx(TRUTH["filters"]["Ha"], abs=0.05)
    # filters without data follow the learned level (default offsets kept)
    d = sfm.DEFAULTS["rc16"]["filters"]
    shift = ((f["filters"]["L"] - d["L"]) + (f["filters"]["Ha"] - d["Ha"])) / 2
    assert f["filters"]["R"] == pytest.approx(d["R"] + shift, abs=1e-3)
    # saturated and below-bias probes stay out of the fit
    junk = [dict(ps[0], adu=65535.0, t="2026-10-24T13:00:00"),
            dict(ps[0], adu=300.0, t="2026-10-24T13:00:01")]
    assert sfm.fit_rig(ps + junk, "rc16")["n"]["L"] == f["n"]["L"]


def test_defaults_match_the_evidence():
    """The shipped defaults (fit to 412 lines of 09-08..10-23): the 10-23
    dusk Ha probe (1100 ADU at 22.5 s, sun -5.2) and the 09-08 dawn SII set
    (5.6-8.7 s at sun -1.3..-0.6) are inside the fit's scatter."""
    rc = sfm.DEFAULTS["rc16"]
    want = math.log((1099.6 - 250) / 22.525)
    got = rc["filters"]["Ha"] + rc["slope"] * -5.2
    assert abs(got - want) < 1.0
    t = sfm.exposure_at(rc, "SII", -1.0)
    assert 3.0 < t < 15.0
    # the Piggy-600's 10-23 dusk probe: 15 s at 18:55 (sun -5.0) gave 43.7 %
    t = sfm.exposure_at(sfm.DEFAULTS["piggyback"], "OSC", -5.0)
    assert 8.0 < t < 30.0


def test_refresh_learns_from_the_night_log_and_reports_the_change(tmp_path):
    cfg = _cfg(tmp_path)
    log = Path(cfg.nina_logs_dir) / "20261023-114520-3.2.0.9001.1-202610.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join(_lines(cfg, "L", "L", datetime(2026, 10, 24, 6, 50), n=20)
                             + _lines(cfg, "H", "Ha", datetime(2026, 10, 24, 7, 12), n=20))
                   + "\n", encoding="utf-8")
    res = sfm.refresh(cfg, NIGHT, with_files=False)
    assert res["added"] == 40
    assert any(c.startswith("RC16 L: ") and "+20 probes" in c for c in res["changes"])
    m = sfm.load(cfg)
    assert m["source"] == "learned" and len(m["probes"]) == 40
    assert m["history"][-1]["night"] == NIGHT
    # the same night again adds nothing
    assert sfm.refresh(cfg, NIGHT, with_files=False)["added"] == 0
    # the plan now follows the learned fit
    s = sfm.summary(cfg)
    assert s["source"] == "learned" and s["rigs"]["rc16"]["filters"]["L"]["points"] == 20


# ---- the plan ----------------------------------------------------------------------

def test_dusk_order_narrowband_then_rgb_then_l(tmp_path):
    cfg = _cfg(tmp_path)
    p = fp.plan(cfg, "dusk", NIGHT, ALL7, osc=True)
    order = [s["filter"] for s in p["rigs"]["rc16"]["slots"]]
    assert order[:3] == ["OIII", "Ha", "SII"] and order[-1] == "L"
    assert set(order[3:6]) == {"R", "G", "B"}
    starts = [s["start"] for s in p["rigs"]["rc16"]["slots"]]
    assert starts == sorted(starts)
    for s in p["rigs"]["rc16"]["slots"]:
        w0, w1 = s["window"]
        assert w0 <= s["start"] <= w1
        # the sun is higher at the start than at the window end (darkening)
        assert sfm.sun_alt(cfg, w0) > sfm.sun_alt(cfg, w1)
        assert s["attempts"] == 2 and s["min_exp"] == 1.0 and s["max_exp"] == 30.0
    # the flat spot is opposite the setting sun: east, alt 75
    pt = p["pointing"]
    assert pt["alt"] == 75 and 50 < pt["az"] < 110
    assert abs(((pt["az"] - pt["sun_az"]) % 360) - 180) < 0.01
    assert p["rigs"]["piggyback"]["slots"]


def test_dusk_late_roof_skips_what_has_passed(tmp_path):
    """10-23: the roof read safe at sunset + 17. Narrowband (sun +2..-1)
    and R / G have passed; what still fits is shot; the rest stays owed."""
    cfg = _cfg(tmp_path)
    tw = fp.twilight(cfg, NIGHT)
    p = fp.plan(cfg, "dusk", NIGHT, ALL7, osc=True,
                ready=tw["sunset"] + timedelta(minutes=17))
    rc = p["rigs"]["rc16"]
    shot = [s["filter"] for s in rc["slots"]]
    assert shot and set(shot) <= {"B", "L"}
    for f in ("Ha", "OIII", "SII", "R"):
        assert rc["skipped"][f].startswith("window passed")
    assert p["rigs"]["piggyback"]["slots"]      # the OSC still fits (15-25 s)
    # nothing at dusk without the late-exposure allowance: a 10 s dusk max
    tight = _cfg(tmp_path / "t", flats_dusk_exposure_max_s=10.0)
    q = fp.plan(tight, "dusk", NIGHT, ALL7, ready=tw["sunset"] + timedelta(minutes=17))
    assert q["rigs"]["rc16"]["slots"] == []


def test_dawn_order_and_the_hard_end(tmp_path):
    cfg = _cfg(tmp_path)
    tw = fp.twilight(cfg, NIGHT)
    p = fp.plan(cfg, "dawn", NIGHT, ALL7, osc=True)
    order = [s["filter"] for s in p["rigs"]["rc16"]["slots"]]
    assert order[0] == "L" and set(order[1:4]) == {"B", "G", "R"}
    assert set(order[4:]) == {"SII", "Ha", "OIII"} and order[-1] == "OIII"
    assert p["hard_end"] == tw["sunrise"] + timedelta(minutes=20)
    assert all(s["stop"] <= p["hard_end"] for s in p["rigs"]["rc16"]["slots"])
    assert all(s["attempts"] == 4 for s in p["rigs"]["rc16"]["slots"])
    # broadband well after nautical dawn + 5 (10-08/13/15: all too dim there)
    first = p["rigs"]["rc16"]["slots"][0]["start"]
    assert first > tw["naut_dawn"] + timedelta(minutes=25)
    # the flat spot opposite the rising sun: west
    assert 250 < p["pointing"]["az"] < 310
    # a hard end at sunrise leaves the late narrowband owed
    early = _cfg(tmp_path / "e", flats_dawn_end_after_sunrise_min=0)
    q = fp.plan(early, "dawn", NIGHT, ALL7)
    assert "OIII" in q["rigs"]["rc16"]["skipped"]


def test_plan_store_round_trip(tmp_path):
    cfg = _cfg(tmp_path)
    p = fp.plan(cfg, "dawn", NIGHT, ["L"], osc=False)
    fp.save_plan(cfg, p, dispatched={"rc16": True})
    back = fp.load_plan(cfg, NIGHT, "dawn")
    assert back["end"] == p["end"] and back["rigs"]["rc16"]["slots"][0]["filter"] == "L"
    fp.mark(cfg, NIGHT, "dawn", reported="x")
    assert fp.load_plan(cfg, NIGHT, "dawn")["reported"] == "x"


# ---- owed carry-over ------------------------------------------------------------------

def _flat(path: Path, flt: str, when_utc: datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    h["IMAGETYP"] = "FLAT"
    h["FILTER"] = flt
    fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16), header=h).writeto(path)
    ts = calendar.timegm(when_utc.timetuple())
    os.utime(path, (ts, ts))


def _shoot(cfg, night, slot, n, raw):
    d = Path(cfg.image_watch_dir) / night / "FLAT"
    for i in range(n):
        _flat(d / f"{raw}_{slot['start']:%H%M}_{i:04d}.fits", raw,
              slot["start"] + timedelta(seconds=20 * i))


def test_dusk_frames_leave_the_dawn_and_vice_versa(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    tw = fp.twilight(cfg, NIGHT)
    p = fp.plan(cfg, "dusk", NIGHT, ["B", "L"],
                ready=tw["sunset"] + timedelta(minutes=10))
    fp.save_plan(cfg, p, dispatched_at=tw["sunset"], dispatched={"rc16": True})
    slots = {s["filter"]: s for s in p["rigs"]["rc16"]["slots"]}
    _shoot(cfg, NIGHT, slots["B"], 15, "B")
    _shoot(cfg, NIGHT, slots["L"], 4, "L")         # a cut set: still owed
    assert dk.window_counts(cfg, NIGHT, "dusk")["rc16"] == {"B": 15, "L": 4}
    now = tw["sunset"] + timedelta(hours=2)
    monkeypatch.setattr(cal, "flats_shot_today", lambda c: set())
    assert cal.flats_covered(cfg, "rc16", now=now) == {"B"}
    # the night sequence's dawn filters leave B out (flats_when both)
    from photonscript.scheduler import nina_sequence_json as nsj
    from photonscript.shared.models import FilterType
    monkeypatch.setattr(cal, "stale_flat_filters", lambda c: ["B", "L", "R"])
    monkeypatch.setattr(cal, "flats_covered", lambda c, rig, now=None: {"B"})
    seq = type("S", (), {"targets": []})()
    got = [f.value for f in nsj.dawn_flat_filters(seq, cfg)]
    assert got == ["L", "R"]
    assert FilterType("B") not in nsj.dawn_flat_filters(seq, cfg)


def test_dawn_counts_leave_the_next_dusk(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    prev = "2026-10-22"
    p = fp.plan(cfg, "dawn", prev, ["L", "R"])
    fp.save_plan(cfg, p, dispatched={"rc16": True})
    sl = {s["filter"]: s for s in p["rigs"]["rc16"]["slots"]}
    _shoot(cfg, prev, sl["L"], 15, "L")
    monkeypatch.setattr(cal, "stale_flat_filters", lambda c: ["L", "R"])
    state = {"night": NIGHT}
    stale, _pb = df._stale(cfg, state)
    assert stale == ["R"]


# ---- the window report -----------------------------------------------------------------

def _fail_lines(t_local, flt):
    return [f"{t_local:%Y-%m-%dT%H:%M:%S}.1|INFO|SequenceItem.cs|Run|208|Starting "
            "Category: Flat Device, Container: NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat",
            f"{t_local:%Y-%m-%dT%H:%M:%S}.2|ERROR|SequenceItem.cs|Run|263|x - ",
            "NINA.Core.Model.SequenceEntityFailedException: Failed to determine "
            "exposure time for sky flats - sky is too dim.",
            f"{t_local:%Y-%m-%dT%H:%M:%S}.3|ERROR|SequenceItem.cs|RunErrorBehavior|195|"
            f"Instruction Sky flats {flt} failed after 2 attempts. Error behavior is "
            "set to ContinueOnError. Continuing."]


def test_window_report_counts_frames_and_says_why(tmp_path):
    cfg = _cfg(tmp_path)
    tw = fp.twilight(cfg, NIGHT)
    p = fp.plan(cfg, "dusk", NIGHT, ["Ha", "B", "L"],
                ready=tw["sunset"] + timedelta(minutes=10))
    fp.save_plan(cfg, p, dispatched_at=tw["sunset"], dispatched={"rc16": True})
    slots = {s["filter"]: s for s in p["rigs"]["rc16"]["slots"]}
    _shoot(cfg, NIGHT, slots["B"], 15, "B")
    lt = fp.to_local(cfg, slots["L"]["start"])
    log = Path(cfg.nina_logs_dir) / "20261023-114520-3.2.0.9001.1-202610.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join([
        "2026-10-23T18:00:00.0|INFO|SafetyMonitorVM.cs|UpdateMonitorValues|95|"
        "SafetyMonitorInfo state changed to Safe"] + _fail_lines(lt, "L")) + "\n",
        encoding="utf-8")
    rep = dk.window_report(cfg, NIGHT, "dusk",
                           {"changes": ["RC16 B: 4.47 s at sun -3.3 deg (was -3.1)"]})
    line = rep["line"]
    assert line.startswith(f"Dusk flats {NIGHT}: RC16 B 15.")
    assert "RC16 L: no exposure, sky too dim" in line
    assert "RC16 Ha: not planned: window passed" in line
    assert "Sky model: RC16 B: 4.47 s" in line
    assert "complete" not in line


def test_window_report_none_saved_roof_closed(tmp_path):
    cfg = _cfg(tmp_path)
    p = fp.plan(cfg, "dawn", NIGHT, ["L"])
    fp.save_plan(cfg, p, dispatched={"rc16": True})
    log = Path(cfg.nina_logs_dir) / "20261023-114520-3.2.0.9001.1-202610.log"
    log.parent.mkdir(parents=True)
    log.write_text("\n".join([
        "2026-10-23T21:00:00.0|INFO|SafetyMonitorVM.cs|UpdateMonitorValues|95|"
        "SafetyMonitorInfo state changed to Safe",
        "2026-10-24T04:25:21.0|INFO|SafetyMonitorVM.cs|UpdateMonitorValues|95|"
        "SafetyMonitorInfo state changed to Unsafe",
        "2026-10-24T06:22:46.0|INFO|SequenceItem.cs|Run|208|Starting Category: x"]) + "\n",
        encoding="utf-8")
    rep = dk.window_report(cfg, NIGHT, "dawn")
    assert rep["line"] == (f"Dawn flats {NIGHT}: none saved (RC16 L: roof closed at "
                           "the flat window (closed 04:25)).")


def test_report_tick_pushes_once_after_the_window(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    p = fp.plan(cfg, "dawn", NIGHT, ["L"])
    fp.save_plan(cfg, p, dispatched={"rc16": True})
    monkeypatch.setattr(sfm, "refresh", lambda c, night, **kw: {"added": 0, "changes": []})
    pushes = []

    async def notify(c, msg, title="", **kw):
        pushes.append((title, msg))
    run = lambda now: asyncio.new_event_loop().run_until_complete(  # noqa: E731
        df.maybe_report_windows(cfg, notify, now=now))
    assert run(p["end"] + timedelta(minutes=5)) == [] and pushes == []
    out = run(p["end"] + timedelta(minutes=15))
    assert out and out[0].startswith(f"Dawn flats {NIGHT}: none saved")
    assert pushes == [("PhotonScript flats", out[0])]
    assert run(p["end"] + timedelta(minutes=30)) == []        # once
    assert fp.load_plan(cfg, NIGHT, "dawn")["reported"]


# ---- the sequences lint clean ---------------------------------------------------------

def test_plan_sequences_lint_clean(tmp_path):
    from photonscript.scheduler.sequence_lint import lint
    from photonscript.scheduler.sideload import lint_companion
    from photonscript.shared.rigs import rig_config
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    tw = fp.twilight(cfg, NIGHT)
    now = tw["sunset"] - timedelta(minutes=40)
    p = fp.plan(cfg, "dusk", NIGHT, ["Ha", "R", "L"], osc=True, ready=now)
    rc = json.loads(cal.generate_dusk_flats_json(
        cfg, only_filters=["Ha", "R", "L"], hold_for_osc=True, now=now, plan=p)[0])
    # the night rules (targets, meridian, tracking) do not apply to a flats run
    bad = [f for f in lint(rc).findings if f.level == "ERROR" and f.rule in (
        "loop-spin", "parent-links", "safety", "flat-filter")]
    assert not bad, bad
    names = [n.get("Name", "") for n in _walk(rc) if str(n.get("Name", "")).startswith("FLATS ")]
    assert [n.split()[1] for n in names] == [s["filter"] for s in p["rigs"]["rc16"]["slots"]]
    assert len([n for n in _walk(rc) if n.get("$type", "").startswith(_SKYFLAT)]) == 3
    slew = next(n for n in _walk(rc) if "SlewScopeToAltAz" in n.get("$type", ""))
    assert slew["Coordinates"]["AltDegrees"] == 75
    pb = json.loads(cal.generate_dusk_flats_json(
        rig_config(cfg, "piggyback"), osc=True, owns_mount=False, safety=True,
        now=now, plan=p)[0])
    assert lint_companion(pb).ok


def test_night_with_the_dawn_plan_passes_lint_and_the_dispatch_gate(tmp_path, monkeypatch):
    """The night sequence's End area (pointing, per-filter waits and stops,
    the hold for the Piggy-600) keeps the night lint and the PS-197 gate
    clean."""
    from photonscript.scheduler import dispatch_gate
    from photonscript.scheduler import nina_sequence_json as nsj
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.scheduler.sequence_lint import lint
    from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: cfg)
    monkeypatch.setattr(cal, "stale_flat_filters", lambda c: ["L", "B"])
    t = NinaSequenceTarget(name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                           exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                   exposure_seconds=300, count=40,
                                                   gain=200, offset=256)],
                           camera_temp_c=0.0)
    seq = json.loads(nsj.generate_nina_json(build_sequence_for_night("FV2", [t])))
    block = next(n for n in _walk(seq) if str(n.get("Name", "")).startswith("DAWN_SKY_FLATS"))
    sets = [n["Name"] for n in _walk(block) if n.get("$type", "").startswith(_SKYFLAT)]
    assert sets == ["Sky flats L", "Sky flats B", "Sky flats Ha"]
    errs = [f for f in lint(seq, guided=False).findings if f.level == "ERROR"]
    assert not errs, errs
    assert dispatch_gate.check(seq, guided=False)["ok"]
