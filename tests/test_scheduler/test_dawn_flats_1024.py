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
sky brightens, the closed-roof skip stays, and the morning report says
"Dawn flats <night>: RC16 N shot, M filed (why)". Flats v2 (same day): each
set runs in its planned sun-altitude window (flat_plan.py), the block points
opposite the sun and ends at the plan's hard end; no "complete" push.
"""
import calendar
import json
import os
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
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
from tests.test_scheduler import fake_sky
from tests.test_scheduler.test_ps77_safety_stop import (
    DUSK_LOCAL, TIMES, NinaSim, _gen, _short, _vals, _walk)

_SKYFLAT = "NINA.Sequencer.SequenceItem.FlatDevice.SkyFlat"


def _named(seq, prefix):
    return next(d for d in _walk(seq) if str(d.get("Name") or "").startswith(prefix))


def _kinds(node):
    return [_short(c["$type"]) for c in _vals(node, "Conditions")]


# ---- the sequences (2026-10-24 flats v2: the plan by sun altitude) ---------------
# The sim's clock (test_ps77_safety_stop): astro dusk 20:18 local, nautical
# dawn 05:48 local. FAKE_SUNSET puts the synthetic sun's nautical dawn on
# 2026-10-24 near 11:48Z = 05:48 MDT, so the plan's fixed clock times line up.

FAKE_SUNSET = datetime(2026, 10, 24, 1, 1)


@pytest.fixture
def sky(tmp_path, monkeypatch):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             observatory_tz="America/Denver")
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: cfg)
    monkeypatch.setattr(cal, "stale_flat_filters", lambda c: [])
    info = fake_sky.install(monkeypatch, FAKE_SUNSET)
    real = cal.dawn_flat_plan
    monkeypatch.setattr(cal, "dawn_flat_plan", lambda c, f, o, now=None, night=None:
                        real(c, f, o, night="2026-10-23"))
    return {"cfg": cfg, **info}


def _sim_s(hh, mm):
    """A local clock time as sim seconds after astro dusk."""
    s = 3600 * hh + 60 * mm
    return (s + 24 * 3600 if s < 12 * 3600 else s) - DUSK_LOCAL


def test_rc16_dawn_block_follows_the_plan(sky):
    seq = _gen(oiii=200, ha=200)
    block = _named(seq, "DAWN_SKY_FLATS")
    # closed roof still skips the block, it runs once, and ends by itself at
    # the plan's hard end (sunrise + flats_dawn_end_after_sunrise_min)
    assert _kinds(block) == ["SafetyMonitorCondition", "LoopCondition", "TimeCondition"]
    end = _vals(block, "Conditions")[2]
    assert "TimeProvider" in end["SelectedProvider"]["$type"]
    plan = cal.dawn_flat_plan(sky["cfg"], ["OIII", "Ha"], False)
    from photonscript.scheduler.flat_plan import to_local
    he = to_local(sky["cfg"], plan["hard_end"])
    assert (end["Hours"], end["Minutes"]) == (he.hour, he.minute)
    items = _vals(block, "Items")
    kinds = [_short(d["$type"]) for d in items]
    # pointed at the flat spot opposite the (morning, east) sun at once,
    # tracking off, then each set at its own start
    assert kinds[:3] == ["SendToPushover", "SlewScopeToAltAz", "SetTracking"]
    slew = items[1]["Coordinates"]
    assert slew["AltDegrees"] == 75 and slew["AzDegrees"] == 285
    sets = [d for d in _walk(block) if d.get("$type", "").startswith(_SKYFLAT)]
    # the brightening sky: Ha (needs less light than OIII here) first
    assert [s["Name"] for s in sets] == ["Sky flats Ha", "Sky flats OIII"]
    assert all(s["Attempts"] == 4 and s["MinExposure"] == 1.0
               and s["MaxExposure"] == 30.0 for s in sets)
    # nothing in the block says "complete"
    assert "complete" not in json.dumps(block)


def test_dusk_and_calibration_sets_keep_one_try():
    assert nsj._sky_flat(FilterType.LUMINANCE, 15, 200, 256)["Attempts"] == 1
    assert cal._osc_sky_flat(25, 100, 256)["Attempts"] == 1
    assert nsj._sky_flat(FilterType.LUMINANCE, 15, 200, 256, attempts=3)["Attempts"] == 3


def test_attempts_follow_config():
    assert cal.dawn_flat_attempts(PhotonScriptConfig(dawn_flat_attempts=1)) == 1
    assert cal.dawn_flat_attempts(PhotonScriptConfig(dawn_flat_attempts=0)) == 1


def test_osc_dawn_set_at_its_planned_window(sky):
    cfg = rigs.rig_config(PhotonScriptConfig(
        _env_file=None, data_dir=sky["cfg"].data_dir,
        observatory_tz="America/Denver",
        piggyback_enabled=True, piggyback_default_gain=100,
        piggyback_default_offset=256, piggyback_dark_exposures="120",
        piggyback_setpoint_c=0.0, piggyback_image_lights=True,
        piggyback_focus_seed=0), "piggyback")
    plan = cal.dawn_flat_plan(cfg, [], True)
    slot = plan["rigs"]["piggyback"]["slots"][0]
    from photonscript.scheduler.flat_plan import to_local
    st, sp = to_local(cfg, slot["start"]), to_local(cfg, slot["stop"])
    from photonscript.scheduler.sideload import lint_companion
    for safety in (True, False):
        root = json.loads(cal.generate_piggyback_companion_json(
            cfg, has_safety=safety, with_lights=True))
        block = _named(root, "DAWN_SKY_FLATS_OSC")
        items = _vals(block, "Items")
        wait = items[0]
        assert _short(wait["$type"]) == "WaitForTime"
        assert (wait["Hours"], wait["Minutes"]) == (st.hour, st.minute)
        box = items[-1]
        want = (["SafetyMonitorCondition"] if safety else []) + [
            "LoopCondition", "TimeCondition"]
        assert _kinds(box) == want
        stop = _vals(box, "Conditions")[-1]
        assert (stop["Hours"], stop["Minutes"]) == (sp.hour, sp.minute)
        if safety:
            assert items[1]["Name"] == "WAIT_SAFE_FOR_OSC_FLATS"
        sf = next(d for d in _walk(block) if d.get("$type", "").startswith(_SKYFLAT))
        assert sf["Attempts"] == 4
        assert lint_companion(root).ok
        assert "complete" not in json.dumps(block)


# ---- behavior (the PS-77 NINA interpreter) -------------------------------------

class _SlowFlats(NinaSim):
    """Sky flats that take 60 s each (a long dawn): each set must still end
    at its own stop and the block by the hard end."""

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


def test_sim_long_flats_end_at_their_stops_then_park(sky):
    sim = _SlowFlats(_gen(oiii=200, ha=200)).run()
    plan = cal.dawn_flat_plan(sky["cfg"], ["OIII", "Ha"], False)
    from photonscript.scheduler.flat_plan import to_local
    slots = plan["rigs"]["rc16"]["slots"]
    st = to_local(sky["cfg"], slots[0]["start"])
    sp = to_local(sky["cfg"], slots[-1]["stop"])
    he = to_local(sky["cfg"], plan["hard_end"])
    assert sim.flats and min(sim.flats) >= _sim_s(st.hour, st.minute)
    assert max(sim.flats) < _sim_s(sp.hour, sp.minute) + 60
    nd = TIMES["NauticalDawnProvider"]
    end_parks = [t for t in sim.parks if t >= nd]
    assert end_parks and min(end_parks) <= _sim_s(he.hour, he.minute) + 60


def test_sim_closed_roof_at_dawn_still_shoots_no_flat(sky):
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
