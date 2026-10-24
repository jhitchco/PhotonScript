"""2026-10-24: no dawn sky flat saved on either rig since 09-07 (RC16) / 09-21.

What the NINA logs show:
  * 10-22, 10-23, 10-24 mornings: the roof was closed at the flat window
    (all night 10-21/22; closed 00:23 on 10-23 and 04:25 on 10-24). The RC16
    reached DAWN_SKY_FLATS at 06:22 and its SafetyMonitorCondition skipped it
    (correct: closed roof = junk flats); the Piggy-600 waited for ND + 20 and
    the armer's dawn shutdown (paused, astro dawn + 30) stopped NINA #2 at
    06:35. Nothing to fix there, but the morning report said nothing.
  * the clear dawns (10-08, 10-13, 10-15): every RC16 and OSC set was tried
    ONCE from ND + 5 (OSC ND + 20) and NINA's SkyFlat gave up "sky is too
    dim" at its 30 s cap (RC16 L 2076-2782 ADU, OSC 9581-11978 at 30 s,
    against a 40 % floor of ~26000) after 1-11 min, ContinueOnError: no
    frame saved. The roof stayed open until ~07:45-07:51 (sunrise + 30).

The fix pinned here: each dawn set is retried (dawn_flat_attempts) while the
sky brightens, both dawn flat blocks end by themselves at ND + 5 +
dawn_flats_window_min - 3 (before the armer's unchanged backstop, so the
park is never later), the closed-roof skip stays, and the morning report
says "Dawn flats <night>: RC16 N shot, M filed (why)".
"""
import calendar
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
from astropy.io import fits

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import dusk_flats_check as dk
from photonscript.scheduler import morning_report as mr
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.calibration_qa import QUARANTINE_DIR
from photonscript.scheduler.runs import library_root
from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import FilterType
from photonscript.shared.rigs import rig_label
from tests.test_scheduler.test_ps77_safety_stop import (
    TIMES, NinaSim, _gen, _short, _vals, _walk)

_SKYFLAT = "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat"


def _named(seq, prefix):
    return next(d for d in _walk(seq) if str(d.get("Name") or "").startswith(prefix))


def _kinds(node):
    return [_short(c["$type"]) for c in _vals(node, "Conditions")]


# ---- the sequences ----------------------------------------------------------------

def test_rc16_dawn_sets_retry_and_the_block_ends_before_the_backstop():
    seq = _gen(oiii=200, ha=200)
    block = _named(seq, "DAWN_SKY_FLATS")
    # closed roof still skips the block; it still runs once; and now ends itself
    assert _kinds(block) == ["SafetyMonitorCondition", "LoopCondition", "TimeCondition"]
    end = _vals(block, "Conditions")[2]
    assert "NauticalDawnProvider" in end["SelectedProvider"]["$type"]
    cfg = PhotonScriptConfig()
    assert end["MinutesOffset"] == 5 + cfg.dawn_flats_window_min - cal.DAWN_FLAT_END_MARGIN_MIN
    sets = [d for d in _walk(block) if d.get("$type", "").startswith(_SKYFLAT)]
    assert sets and all(s["Attempts"] == cfg.dawn_flat_attempts == 4 for s in sets)
    # the flat window itself did not move: ND + 5
    wait = next(d for d in _vals(block, "Items") if _short(d["$type"]) == "WaitForTime")
    assert wait["MinutesOffset"] == 5


def test_dusk_and_calibration_sets_keep_one_try():
    assert nsj._sky_flat(FilterType.LUMINANCE, 15, 200, 256)["Attempts"] == 1
    assert cal._osc_sky_flat(25, 100, 256)["Attempts"] == 1
    assert nsj._sky_flat(FilterType.LUMINANCE, 15, 200, 256, attempts=3)["Attempts"] == 3


def test_attempts_and_deadline_follow_config():
    assert cal.dawn_flat_attempts(PhotonScriptConfig(dawn_flat_attempts=1)) == 1
    assert cal.dawn_flat_attempts(PhotonScriptConfig(dawn_flat_attempts=0)) == 1
    assert cal.dawn_flat_deadline_offset(PhotonScriptConfig(dawn_flats_window_min=30)) == 32
    # window off: the armer shuts down at astro dawn + 30, no flat time
    assert cal.dawn_flat_deadline_offset(PhotonScriptConfig(dawn_flats_window_min=0)) == 5
    # the deadline always sits DAWN_FLAT_END_MARGIN_MIN before the backstop
    for win in (10, 40, 60):
        off = cal.dawn_flat_deadline_offset(PhotonScriptConfig(dawn_flats_window_min=win))
        assert off + cal.DAWN_FLAT_END_MARGIN_MIN == 5 + win


def test_osc_dawn_set_retries_inside_the_same_deadline():
    cfg = rigs.rig_config(PhotonScriptConfig(
        piggyback_enabled=True, piggyback_default_gain=100,
        piggyback_default_offset=256, piggyback_dark_exposures="120",
        piggyback_setpoint_c=0.0, piggyback_image_lights=True,
        piggyback_focus_seed=0), "piggyback")
    for safety in (True, False):
        root = json.loads(cal.generate_piggyback_companion_json(
            cfg, has_safety=safety, with_lights=True))
        block = _named(root, "DAWN_SKY_FLATS_OSC")
        want = (["SafetyMonitorCondition"] if safety else []) + [
            "LoopCondition", "TimeCondition"]
        assert _kinds(block) == want
        assert _vals(block, "Conditions")[-1]["MinutesOffset"] == 42
        sf = next(d for d in _walk(block) if d.get("$type", "").startswith(_SKYFLAT))
        assert sf["Attempts"] == 4
    # the OSC start (ND + 20) stays inside the window
    assert cal.osc_flat_dawn_offset(cfg) < cal.dawn_flat_deadline_offset(cfg)


# ---- behavior (the PS-77 NINA interpreter) -------------------------------------

class _SlowFlats(NinaSim):
    """Sky flats that take 60 s each (a long dawn): without its own end the
    block would run all 15 per set past the armer's backstop."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.flats, self.parks = [], []

    def duration(self, n):
        if n is not None and n.type == "TakeExposure" and n.d.get("ImageType") == "FLAT":
            return 60.0
        return super().duration(n)

    def run_instruction(self, n):
        if n.type == "TakeExposure" and n.d.get("ImageType") == "FLAT":
            self.flats.append(self.t)
        if n.type == "ParkScope":
            self.parks.append(self.t)
        return super().run_instruction(n)


def test_sim_long_flats_end_at_the_deadline_then_park():
    sim = _SlowFlats(_gen(oiii=200, ha=200)).run()
    nd = TIMES["NauticalDawnProvider"]
    cfg = PhotonScriptConfig()
    deadline = nd + 60 * cal.dawn_flat_deadline_offset(cfg)
    backstop = nd + 60 * (5 + cfg.dawn_flats_window_min)
    assert sim.flats and min(sim.flats) >= nd + 5 * 60
    assert max(sim.flats) < deadline
    # the End area parks right after the block ends, before the backstop
    end_parks = [t for t in sim.parks if t >= nd]
    assert end_parks and deadline <= min(end_parks) < backstop


def test_sim_closed_roof_at_dawn_still_shoots_no_flat():
    nd = TIMES["NauticalDawnProvider"]
    # 10-24: closed 04:25 local, still closed through dawn
    sim = _SlowFlats(_gen(oiii=200, ha=200), unsafe=[(nd - 7200, nd + 4 * 3600)]).run()
    assert sim.flats == []


# ---- the morning check: dawn shot vs filed and why ------------------------------

TW = {"astro_dawn": datetime(2026, 10, 24, 12, 5), "naut_dawn": datetime(2026, 10, 24, 12, 31),
      "sunrise": datetime(2026, 10, 24, 13, 25)}          # 06:05 / 06:31 / 07:25 MDT
DATE = "2026-10-23"


def _cfg(tmp_path):
    return PhotonScriptConfig(
        _env_file=None, data_dir=str(tmp_path / "data"),
        image_watch_dir=str(tmp_path / "nina"),
        nina_logs_dir=str(tmp_path / "logs1"),
        piggyback_enabled=True,
        piggyback_image_watch_dir=str(tmp_path / "nina2"),
        piggyback_nina_logs_dir=str(tmp_path / "logs2"),
        observatory_tz="America/Denver", evening_forecast_enabled=False)


def _log(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _flat(path: Path, flt: str, when_utc: datetime):
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    h["IMAGETYP"] = "FLAT"
    h["FILTER"] = flt
    fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16), header=h).writeto(path)
    ts = calendar.timegm(when_utc.timetuple())
    os.utime(path, (ts, ts))


def _ln(t, text):
    return f"2026-10-24T{t}.0000|INFO|{text}"


RUN = "SequenceItem.cs|Run|208|Starting Category: Utility, Item: WaitForTime"
SAFE = "SafetyMonitorVM.cs|UpdateMonitorValues|95|SafetyMonitorInfo state changed to Safe"
UNSAFE = "SafetyMonitorVM.cs|UpdateMonitorValues|95|SafetyMonitorInfo state changed to Unsafe"
COND_UNSAFE = "SafetyMonitorCondition.cs|Check|96|SafetyMonitorCondition finished. Status=Unsafe"
CANCEL = "Sequencer.cs|Start|85|Sequence run was cancelled"
SKYFLAT = ("SkyFlat.cs|Execute|219|Determining Sky Flat Exposure Time. Min 0.1, "
           "Max 30, Target 50%, Tolerance 20%")


def _fail(t, f, how="dim"):
    return [f"2026-10-24T{t}.1|ERROR|SequenceItem.cs|Run|263|Category: Flat Device, "
            "Container: NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat - ",
            "NINA.Core.Model.SequenceEntityFailedException: Failed to determine "
            f"exposure time for sky flats - sky is too {how}.",
            f"2026-10-24T{t}.2|ERROR|SequenceItem.cs|RunErrorBehavior|195|Instruction "
            f"Sky flats {f} failed after 4 attempts. Error behavior is set to "
            "ContinueOnError. Continuing."]


def test_dawn_1024_roof_closed_at_the_window_on_both_rigs(tmp_path):
    cfg = _cfg(tmp_path)
    # the 10-24 morning as logged: Safe at dusk, Unsafe 04:25, the RC16 End
    # area skips the flats 06:22, the paused dawn shutdown stops NINA #2 06:35
    _log(tmp_path / "logs1" / "20261023-114520-3.2.0.9001.1-202610.log", [
        "2026-10-23T21:15:06.0000|INFO|" + SAFE,
        _ln("04:25:21", UNSAFE), _ln("06:22:46", COND_UNSAFE),
        _ln("06:22:47", "SequenceItem.cs|Run|208|Starting Category: Telescope, Item: ParkScope")])
    _log(tmp_path / "logs2" / "20261023-114535-3.2.0.9001.2-202610.log", [
        "2026-10-23T21:15:06.0000|INFO|" + SAFE,
        _ln("04:25:20", UNSAFE), _ln("06:02:54", RUN), _ln("06:35:34", CANCEL)])
    line = dk.dawn_morning_line(cfg, DATE, TW)
    pig = rig_label(cfg, "piggyback")
    assert line == (
        f"Dawn flats {DATE}: RC16 0 shot, 0 filed (roof closed at the flat window, "
        f"closed 04:25); {pig} 0 shot, 0 filed (roof closed at the flat window, "
        "closed 04:25; NINA stopped 06:35 before the 06:52 window)")


def test_dawn_clear_too_dim_counts_shot_filed_and_ignores_the_disconnect(tmp_path):
    cfg = _cfg(tmp_path)
    raw = {v: k for k, v in cfg.reverse_filter_map().items()}
    raw_b = raw.get("B", "B")
    flat_dir = tmp_path / "nina" / "2026-10-24" / "FLAT"
    names = [f"2026-10-24_07-0{i}-00_{raw_b}_0000{i}.fits" for i in range(3)]
    for i, n in enumerate(names):
        _flat(flat_dir / n, raw_b, datetime(2026, 10, 24, 13, i))
    cal_root = library_root(cfg) / "Calibration"
    (cal_root / "FLAT" / "2026-10-24").mkdir(parents=True)
    (cal_root / "FLAT" / "2026-10-24" / names[0]).write_bytes(b"x")
    (cal_root / QUARANTINE_DIR / "FLAT" / "2026-10-24").mkdir(parents=True)
    (cal_root / QUARANTINE_DIR / "FLAT" / "2026-10-24" / names[1]).write_bytes(b"x")
    # 10-15 shape: L and R give up too dim; B shot after a retry; the End
    # area's DisconnectAllEquipment reads Unsafe (not a roof close)
    _log(tmp_path / "logs1" / "20261023-114520-3.2.0.9001.1-202610.log", [
        "2026-10-23T21:15:06.0000|INFO|" + SAFE, _ln("05:57:54", RUN),
        _ln("06:36:33", SKYFLAT)] + _fail("06:47:05", "L") + [
        _ln("06:47:05", SKYFLAT)] + _fail("06:57:32", "R") + [
        _ln("06:57:32", SKYFLAT)] + _fail("06:59:00", "B") + [_ln("06:59:01", SKYFLAT),
        _ln("07:05:38", "DisconnectAllEquipment.cs|Execute|139|Flat Panel is already disconnected"),
        _ln("07:05:38", UNSAFE)])
    rep = dk.dawn_report(cfg, DATE, TW)
    assert [r["rig"] for r in rep["rigs"]] == ["rc16"]       # NINA #2 ran nothing
    rc = rep["rigs"][0]
    assert (rc["shot"], rc["filed"], rc["quarantined"], rc["waiting"]) == (3, 1, 1, 1)
    assert rc["roof"] == "Safe" and rc["closed_local"] is None
    assert rc["failed"] == {"L": "no exposure, sky too dim", "R": "no exposure, sky too dim"}
    assert dk.dawn_morning_line(cfg, DATE, TW) == (
        f"Dawn flats {DATE}: RC16 3 shot, 1 filed (1 quarantined by QA; 1 waiting "
        "for the dawn sweep; L, R: no exposure, sky too dim)")


def test_dawn_nothing_ran_says_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    _log(tmp_path / "logs1" / "20261023-114520-3.2.0.9001.1-202610.log",
         ["2026-10-23T21:15:06.0000|INFO|" + SAFE])
    assert dk.dawn_report(cfg, DATE, TW) is None
    assert dk.dawn_morning_line(cfg, DATE, TW) is None
    assert dk.dawn_morning_line(cfg, DATE, {}) is None


def test_dawn_no_flat_when_the_window_passed_without_a_sky_flat(tmp_path):
    cfg = _cfg(tmp_path)
    _log(tmp_path / "logs1" / "20261023-114520-3.2.0.9001.1-202610.log", [
        "2026-10-23T21:15:06.0000|INFO|" + SAFE, _ln("06:22:46", RUN)])
    assert dk.dawn_morning_line(cfg, DATE, TW) == (
        f"Dawn flats {DATE}: RC16 0 shot, 0 filed (no sky flat in NINA's log)")


def test_morning_card_carries_the_dawn_flats_line(monkeypatch):
    line = "Dawn flats 2026-10-23: RC16 0 shot, 0 filed (roof closed at the flat window)"
    card = {"date": "2026-10-23", "rigs": [], "dusk_flats": "Dusk flats x", "dawn_flats": line}
    lines = mr.card_lines(card)
    assert lines.index(line) == lines.index("Dusk flats x") + 1
    assert mr.card_line_priority(line) == mr.PRI_RIG
