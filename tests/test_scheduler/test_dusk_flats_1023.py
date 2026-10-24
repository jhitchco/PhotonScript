"""2026-10-23: dusk flats dispatched on both rigs, zero frames saved.

NINA's SkyFlat found no exposure for any set (RC16 Ha / OIII "sky is too
dim" at the 30 s cap, 11 min each, then the 19:19 stop cut R and G / B / L
never ran; the Piggy-600's first probe was 43.7 % against a 45-55 % band and
the dimming sky outran the search). These pin the fix: RC16 narrowband
stays for the dawn (dusk_flats_narrowband), a 40-60 % SkyFlat band, a
closing push that does not say "complete", and the read-only morning check
"Dusk flats <night>: RC16 N shot, M filed (...)".
"""
import calendar
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from astropy.io import fits

from photonscript.scheduler import auto_armer as aa
from photonscript.scheduler import calibration as cal
from photonscript.scheduler import dusk_flats as df
from photonscript.scheduler import dusk_flats_check as dk
from photonscript.scheduler import morning_report as mr
from photonscript.scheduler.calibration_qa import QUARANTINE_DIR
from photonscript.scheduler.nina_sequence_json import SKY_FLAT_TOLERANCE, _sky_flat
from photonscript.scheduler.runs import library_root
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import FilterType
from photonscript.shared.phd2_store import night_of
from photonscript.shared.rigs import rig_config, rig_label
from tests.test_scheduler.test_dusk_flats_both_rigs import (  # noqa: F401 - fixture
    _SKYFLAT, _Armer, _cfg, _rows, _tick, _walk, env)


# ---- which filters the dusk run shoots ----------------------------------------
# 2026-10-24 (flats v2): the sky model decides. Narrowband is planned at dusk
# only while its window (before sunset) is still ahead; a late roof / tick
# leaves it owed for the dawn. dusk_flats_narrowband off: never at dusk.

def _late(env, minutes=5):
    return env["tw"]["sunset"] + timedelta(minutes=minutes)


def test_dusk_filters_narrowband_flag(tmp_path):
    owed = ["Ha", "OIII", "SII", "R", "G", "B", "L"]
    assert cal.dusk_flat_filters(_cfg(tmp_path), owed) == owed
    off = _cfg(tmp_path, dusk_flats_narrowband=False)
    assert cal.dusk_flat_filters(off, owed) == ["R", "G", "B", "L"]


def test_late_dispatch_shoots_broadband_and_names_the_dawn_leftovers(env, tmp_path):
    """Dispatched at sunset + 5 (10-23: the roof opened at sunset + 15): the
    narrowband windows (sun +2 .. -1.2 deg) have passed, R and G still fit."""
    cfg = _cfg(tmp_path)
    env["st"]["stale"] = ["Ha", "OIII", "R", "G"]
    armer, fs = _Armer(), {}
    assert _tick(cfg, armer, fs, env, _late(env)) == "dispatched"
    assert [r[0] for r in armer.raw] == ["auto dusk flats (R,G)"]
    assert env["st"]["gen"][0]["only_filters"] == ["R", "G"]
    seq = armer.raw[0][1]
    names = [n["Name"] for n in _walk(seq) if n.get("$type", "").startswith(_SKYFLAT)]
    assert names == ["Sky flats R", "Sky flats G"]
    row = _rows(cfg, env["now"])[-1]
    assert "left for dawn: Ha, OIII" in row["detail"]
    assert dk.wanted_filters(row["detail"]) == ["R", "G"]


def test_only_narrowband_owed_late_is_a_no_window_skip(env, tmp_path):
    cfg = _cfg(tmp_path)
    env["st"]["stale"] = ["Ha", "OIII"]
    armer, fs = _Armer(), {}
    assert _tick(cfg, armer, fs, env, _late(env)) == df.SKIP_NO_WINDOW
    assert armer.raw == [] and fs["done"]
    row = _rows(cfg, env["now"])[-1]
    assert row["skip"] == df.SKIP_NO_WINDOW
    assert "Ha" in row["detail"] and "OIII" in row["detail"]
    assert "owed for the dawn" in row["detail"]


def test_only_narrowband_owed_still_holds_for_the_piggy(env, tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    env["st"]["stale"] = ["Ha"]
    env["st"]["pb"] = {"owed": True, "reasons": ["OSC: stale"], "shot_today": False}
    armer, fs = _Armer(), {}
    assert _tick(cfg, armer, fs, env, _late(env)) == "dispatched"
    assert env["st"]["gen"][0]["only_filters"] == []
    assert env["st"]["gen"][0]["hold_for_osc"] is True


def test_early_roof_plans_narrowband_first(env, tmp_path):
    """Roof open 30 min before sunset: the darkening sky serves the
    narrowband first (brightest need), then broadband."""
    cfg = _cfg(tmp_path)
    env["st"]["stale"] = ["Ha", "OIII", "R"]
    armer, fs = _Armer(), {}
    assert _tick(cfg, armer, fs, env) == "dispatched"
    assert env["st"]["gen"][0]["only_filters"] == ["OIII", "Ha", "R"]
    off = _cfg(tmp_path / "off", dusk_flats_narrowband=False)
    armer2 = _Armer()
    assert _tick(off, armer2, {}, env) == "dispatched"
    assert env["st"]["gen"][1]["only_filters"] == ["R"]


# ---- the SkyFlat band and the closing pushes ---------------------------------

def test_sky_flat_band_takes_the_1023_piggy_probe():
    probe = 28606.4 / 65535          # the Piggy-600's 15 s probe, 18:57 local
    assert abs(probe - 0.5) > 0.5 * 0.1          # missed the old 45-55 % band
    assert abs(probe - 0.5) <= 0.5 * SKY_FLAT_TOLERANCE
    assert _sky_flat(FilterType("L"), 5, 100, 50)["HistogramTolerancePercentage"] \
        == SKY_FLAT_TOLERANCE
    osc = cal._osc_sky_flat(5, 100, 50)
    assert osc["HistogramTolerancePercentage"] == SKY_FLAT_TOLERANCE


def test_dusk_pushes_do_not_claim_complete(env, tmp_path):
    """2026-10-24: no closing push at all from the flat sequences; the
    window report counts the frames (dusk_flats.maybe_report_windows)."""
    cfg = _cfg(tmp_path)
    rc = json.loads(cal.generate_dusk_flats_json(cfg, only_filters=["L"],
                                                 now=env["now"])[0])
    pb = json.loads(cal.generate_dusk_flats_json(rig_config(
        _cfg(tmp_path, piggyback_enabled=True), "piggyback"), osc=True,
        owns_mount=False, now=env["now"])[0])
    for seq in (rc, pb):
        pushes = [n for n in _walk(seq) if "Pushover" in n.get("$type", "")]
        assert len(pushes) == 1                       # the intro only
        text = json.dumps(pushes)
        assert "complete" not in text and "run ended" not in text
        assert "by sun altitude" in text


# ---- the morning check: shot vs filed ------------------------------------------

T0 = datetime(2026, 10, 24, 0, 52, 31)        # 18:52:31 local MDT, 2026-10-23


def _flat(path: Path, flt: str, when_utc: datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    h["IMAGETYP"] = "FLAT"
    h["FILTER"] = flt
    fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16), header=h).writeto(path)
    ts = calendar.timegm(when_utc.timetuple())
    os.utime(path, (ts, ts))


def _log(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _check_cfg(tmp_path):
    return PhotonScriptConfig(
        _env_file=None, data_dir=str(tmp_path / "data"),
        image_watch_dir=str(tmp_path / "nina"),
        nina_logs_dir=str(tmp_path / "logs1"),
        piggyback_enabled=True,
        piggyback_image_watch_dir=str(tmp_path / "nina2"),
        piggyback_nina_logs_dir=str(tmp_path / "logs2"),
        observatory_tz="America/Denver", evening_forecast_enabled=False)


def _dispatch(cfg, detail):
    aa.log_decision(cfg, "dispatched", None, detail, "auto dusk flats", T0,
                    action=aa.ACTION_FLATS)
    return night_of(cfg, T0)


RC_DETAIL = ("RC16: L, Ha, OIII, starts 18:52 local, done by 19:19; "
             "Piggy-600 OSC: dispatched, starts 18:57 local, cooled to 0 C")

FAIL = ["2026-10-23T{t}.1|ERROR|SequenceItem.cs|Run|263|Category: Flat Device, "
        "Container: NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat - ",
        "NINA.Core.Model.SequenceEntityFailedException: Failed to determine "
        "exposure time for sky flats - sky is too {how}.",
        "   at NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat.DetermineExposureTime",
        "2026-10-23T{t}.2|ERROR|SequenceItem.cs|RunErrorBehavior|195|Instruction "
        "Sky flats {f} failed after 1 attempt. Error behavior is set to "
        "ContinueOnError. Continuing."]


def _fail(t, f, how):
    return [ln.format(t=t, f=f, how=how) for ln in FAIL]


def test_check_counts_shot_filed_and_names_ninas_reason(tmp_path):
    cfg = _check_cfg(tmp_path)
    date = _dispatch(cfg, RC_DETAIL)
    assert date == "2026-10-23"
    raw_l = {v: k for k, v in cfg.reverse_filter_map().items()}.get("L", "L")
    flat_dir = tmp_path / "nina" / date / "FLAT"
    names = [f"2026-10-23_18-5{i}-00_{raw_l}_0000{i}.fits" for i in range(3)]
    for i, n in enumerate(names):
        _flat(flat_dir / n, raw_l, T0 + timedelta(minutes=3 + i))
    _flat(flat_dir / "old_L.fits", raw_l, T0 - timedelta(hours=8))   # a dawn set
    cal_root = library_root(cfg) / "Calibration"
    (cal_root / "FLAT" / date).mkdir(parents=True)
    (cal_root / "FLAT" / date / names[0]).write_bytes(b"x")
    (cal_root / QUARANTINE_DIR / "FLAT" / date).mkdir(parents=True)
    (cal_root / QUARANTINE_DIR / "FLAT" / date / names[1]).write_bytes(b"x")
    _log(tmp_path / "logs1" / "20261023-130000-3.2.0.9001.1-202610.log",
         ["2026-10-23T13:00:00.0|INFO|App.cs|Start|1|NINA started"]
         + _fail("15:00:00", "OIII", "bright")          # before the run
         + _fail("19:06:04", "Ha", "dim")
         + ["2026-10-23T21:00:00.0|INFO|x|y|1|later"]
         + _fail("21:30:00", "OIII", "dim"))             # after the run
    _log(tmp_path / "logs2" / "20261023-130000-3.2.0.9001.2-202610.log",
         _fail("19:10:16", "OSC", "bright"))

    rep = dk.night_report(cfg, date)
    rc, pb = rep["rigs"]
    assert (rc["shot"], rc["filed"], rc["quarantined"], rc["waiting"]) == (3, 1, 1, 1)
    assert rc["by_filter"] == {"L": 3}
    assert rc["missing"] == ["Ha", "OIII"]
    assert rc["failed"] == {"Ha": "no exposure, sky too dim"}
    assert (pb["shot"], pb["filed"]) == (0, 0)
    assert pb["failed"] == {"OSC": "no exposure, sky too bright"}
    line = dk.morning_line(cfg, date)
    stop = rep["stop_local"]
    assert line == (
        f"Dusk flats {date}: RC16 3 shot, 1 filed (1 quarantined by QA; 1 "
        "waiting for the dawn sweep; Ha: no exposure, sky too dim; OIII: not "
        f"shot by the {stop} stop); {rig_label(cfg, 'piggyback')} 0 shot, 0 "
        "filed (OSC: no exposure, sky too bright)")


def test_check_says_nothing_without_a_dispatch(tmp_path):
    cfg = _check_cfg(tmp_path)
    assert dk.night_report(cfg, "2026-10-23") is None
    assert dk.morning_line(cfg, "2026-10-23") is None


def test_check_rc16_only_when_the_piggy_was_not_dispatched(tmp_path):
    cfg = _check_cfg(tmp_path)
    date = _dispatch(cfg, "RC16: R, starts 18:52 local, done by 19:19")
    rep = dk.night_report(cfg, date)
    assert [r["rig"] for r in rep["rigs"]] == ["rc16"]
    assert dk.morning_line(cfg, date).startswith(
        f"Dusk flats {date}: RC16 0 shot, 0 filed (R: not shot by the ")


def test_morning_card_carries_the_dusk_flats_line():
    line = "Dusk flats 2026-10-23: RC16 0 shot, 0 filed"
    card = {"date": "2026-10-23", "rigs": [], "dusk_flats": line}
    assert line in mr.card_lines(card)
    assert mr.card_line_priority(line) == mr.PRI_RIG
