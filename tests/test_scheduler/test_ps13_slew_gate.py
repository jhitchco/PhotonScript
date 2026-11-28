"""PS-13: slew_gate wired into grading. Move windows from the PS-67 mount log
(else the RC16 frames), the "Clear of RC16 moves" scorecard check, the live
Piggy-600 grade, the dawn pass and the 2026-09-21 M31 replay."""
import asyncio
import csv
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import slew_gate as sg
from photonscript.shared import mount_log
from photonscript.shared import qa_rules as q
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ImageQualityMetrics, TelescopeState

NIGHT = "2026-09-26"
T0 = datetime(2026, 9, 27, 4, 0, 0)          # 04:00Z = the night of 09-26
FIX = Path(__file__).parent / "fixtures" / "ps13_m31_osc3_cull.csv"


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                stamp_fits_object=False, piggyback_enabled=True,
                observatory_tz="UTC", library_attribute=False)
    base.update(kw)
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    return PhotonScriptConfig(**base)


def _t(sec):
    return T0 + timedelta(seconds=sec)


def _mount(ra_h=0.712, dec=41.27, slewing=False, tracking=True, park=False,
           pier="pierWest"):
    return {"RightAscension": ra_h, "Declination": dec, "Altitude": 60.0,
            "Azimuth": 90.0, "SideOfPier": pier, "Slewing": slewing,
            "TrackingEnabled": tracking, "AtPark": park}


def _log(cfg, events):
    """events: [(sec, mount dict)] through the real MountLogger."""
    ml = mount_log.MountLogger(cfg)
    for sec, m in events:
        ml.observe(m, _t(sec))
    return mount_log.load(cfg, NIGHT)


def _night_with_a_slew(cfg):
    """Tracking from 0 s (heartbeats), a slew 600 to 620 s, a meridian flip
    (pier change) at 1500 s."""
    ev = [(s, _mount()) for s in range(0, 600, 60)]
    ev += [(600, _mount(slewing=True)), (605, _mount(ra_h=0.75, slewing=True)),
           (620, _mount(ra_h=0.75))]
    ev += [(s, _mount(ra_h=0.75)) for s in range(680, 1500, 60)]
    ev += [(1500, _mount(ra_h=0.75, pier="pierEast"))]
    ev += [(s, _mount(ra_h=0.75, pier="pierEast")) for s in range(1560, 2400, 60)]
    return _log(cfg, ev)


# ------------------------------------------------------------- windows

def test_windows_from_mount_log_pad_events_and_merge(tmp_path):
    cfg = _cfg(tmp_path)
    lines = _night_with_a_slew(cfg)
    w = sg.windows_from_mount_log(lines, pad_s=10)
    assert w == [(_t(590), _t(630)), (_t(1490), _t(1510))]
    # a big pad merges nothing here, a tiny one keeps the bare slew
    assert sg.windows_from_mount_log(lines, pad_s=0)[0] == (_t(600), _t(620))
    assert sg.merge_windows([(_t(0), _t(10)), (_t(5), _t(20)),
                             (_t(30), _t(40))]) == [(_t(0), _t(20)),
                                                    (_t(30), _t(40))]


def test_short_centering_slew_seen_only_as_a_big_move(tmp_path):
    cfg = _cfg(tmp_path)
    # the 5 s poll never saw Slewing: two lines 5 s apart, 51' apart
    lines = _log(cfg, [(0, _mount()), (60, _mount()),
                       (65, _mount(dec=41.27 + 51 / 60.0)),
                       (70, _mount(dec=41.27 + 51 / 60.0 + 2 / 60.0))])
    w = sg.windows_from_mount_log(lines, pad_s=10)
    assert w == [(_t(50), _t(75))]      # the 2' nudge is not a move


def test_park_and_unpark_are_windows(tmp_path):
    cfg = _cfg(tmp_path)
    lines = _log(cfg, [(0, _mount()), (100, _mount(tracking=False, park=True)),
                       (400, _mount(tracking=False)), (405, _mount())])
    w = sg.windows_from_mount_log(lines, pad_s=10)
    assert (_t(90), _t(110)) in w and (_t(390), _t(410)) in w


def test_mount_log_coverage(tmp_path):
    cfg = _cfg(tmp_path)
    lines = _log(cfg, [(0, _mount()), (60, _mount())])
    assert sg.mount_log_covers(lines, _t(100), _t(220))
    assert not sg.mount_log_covers(lines, _t(1000), _t(1120))   # poll stopped
    assert not sg.mount_log_covers(lines, _t(-500), _t(-380))   # before the log
    assert sg.mount_log_covers(lines, _t(-60), _t(60))          # line inside
    parked = _log(_cfg(tmp_path / "p"), [(0, _mount(tracking=False, park=True))])
    assert sg.mount_log_covers(parked, _t(3000), _t(3120))      # state holds


def test_sub_straddle_overlap_and_note():
    wins = [(_t(590), _t(630))]
    a = sg.sub_straddle(_t(500), _t(620), wins, "mount-log")
    assert a["overlap_s"] == 30.0 and a["src"] == "mount-log"
    assert "04:09:50 to 04:10:30 UTC" in a["note"] and "mount log" in a["note"]
    assert sg.sub_straddle(_t(630), _t(750), wins, "mount-log")["overlap_s"] == 0
    assert sg.sub_straddle(None, None, wins, "mount-log")["overlap_s"] is None


def test_night_windows_prefer_the_mount_log_then_rc16_frames(tmp_path):
    cfg = _cfg(tmp_path)
    lines = _night_with_a_slew(cfg)
    rc16 = [{"start": _t(0), "end": _t(300), "ra": 10.68, "dec": 41.27},
            {"start": _t(3000), "end": _t(3300), "ra": 11.25, "dec": 41.27}]
    nw = sg.NightWindows(cfg, lines=lines, rc16_frames=rc16)
    a = nw.assess(_t(540), _t(660))
    assert a["src"] == "mount-log" and a["overlap_s"] == 40.0
    assert nw.assess(_t(1000), _t(1120))["overlap_s"] == 0        # clear
    # after the log stops (2340 s + 300 s stale): the RC16 frames say
    # the mount moved between 300 s and 3000 s
    b = nw.assess(_t(2700), _t(2820))
    assert b["src"] == "rc16-frames" and b["overlap_s"] == 120.0
    # no mount log, one RC16 frame: nothing can say
    none = sg.NightWindows(cfg, lines=[], rc16_frames=rc16[:1])
    assert none.assess(_t(100), _t(220)) == {"overlap_s": None, "note": None,
                                             "src": None}


def test_gated_rigs(tmp_path):
    assert sg.gated_rigs(_cfg(tmp_path)) == ["piggyback"]
    assert sg.gated_rigs(_cfg(tmp_path, piggyback_enabled=False)) == []


# ------------------------------------------------------- scorecard check

def test_check_statuses_and_modes(tmp_path):
    t = q.thresholds(_cfg(tmp_path), "piggyback")
    assert t["slew_straddle_mode"] == "fail"
    assert q.slew_straddle_check(None, t).status == q.SKIP
    assert q.slew_straddle_check(0.0, t).status == q.PASS
    c = q.slew_straddle_check(42.4, t, "RC16 move 04:09:50 to 04:10:30 UTC")
    assert c.status == q.FAIL and "42 s" in c.reason and "04:09:50" in c.reason
    for mode, st in (("warn", q.WARN), ("info", q.PASS)):
        tt = q.thresholds(_cfg(tmp_path, qa_slew_straddle_mode=mode), "piggyback")
        assert q.slew_straddle_check(42.4, tt).status == st


def test_evaluate_rejects_a_straddler_and_skips_without_data(tmp_path):
    cfg = _cfg(tmp_path)
    good = dict(hfr=3.0, ecc=0.4, stars=500, background=400.0, exp_s=120.0,
                ccd_temp=0.2, exposure="ok")
    ctx = q.context(cfg, "piggyback")
    card = q.evaluate({**good, "slew_overlap_s": 25.0, "slew_note": "x"}, ctx)
    assert card.drivers == ["slew_straddle"] and card.verdict == q.REJECTED
    rows = [r[0] for r in card.compact()["rows"]]
    assert rows == list(q.CHECKS) and rows[-2:] == ["slew_straddle", "pointing"]
    skip = q.evaluate(good, q.context(cfg, "rc16"))
    row = next(c for c in skip.checks if c.id == "slew_straddle")
    assert row.status == q.SKIP and skip.passed
    exp = next(r for r in q.expand(skip.compact()) if r["id"] == "slew_straddle")
    assert exp["reason"].startswith("not judged")
    # the stored fields feed a rescore
    assert q.metrics_from_record({"slew_overlap_s": 25.0,
                                  "slew_note": "x"})["slew_overlap_s"] == 25.0


def test_regrade_swaps_or_inserts_only_that_row(tmp_path):
    cfg = _cfg(tmp_path)
    t = q.thresholds(cfg, "piggyback")
    good = dict(hfr=3.0, ecc=0.4, stars=500, background=400.0, exp_s=120.0,
                ccd_temp=0.2, exposure="ok")
    card = q.evaluate(good, q.context(cfg, "piggyback"))
    # a card stored before PS-13 has no slew_straddle row
    old_rows = [r for r in card.compact()["rows"] if r[0] != "slew_straddle"]
    rec = {"rig": "piggyback", **card.record_fields(),
           "scorecard": {"v": "ps21.2", "verdict": "approved", "rows": old_rows}}
    f = q.regrade_slew_straddle(rec, 30.0, t, "RC16 move")
    assert f["passed_qa"] is False and f["drivers"] == ["slew_straddle"]
    assert [r[0] for r in f["scorecard"]["rows"]] == list(q.CHECKS)
    assert f["slew_overlap_s"] == 30.0 and f["slew_note"] == "RC16 move"
    rec.update(f)
    assert q.regrade_slew_straddle(rec, 30.0, t, "RC16 move") is None
    back = q.regrade_slew_straddle(rec, 0.0, t)
    assert back["passed_qa"] is True and back["slew_note"] is None
    assert q.regrade_slew_straddle({"rig": "piggyback"}, 5.0, t) is None


# ----------------------------------------------------------- dawn pass

def _approved(cfg, name, start, rig="piggyback", **kw):
    good = dict(hfr=3.0, ecc=0.4, stars=500, background=400.0, exp_s=120.0,
                ccd_temp=0.2, exposure="ok")
    card = q.evaluate(good, q.context(cfg, rig))
    return {"rig": rig, "file": name, "target": "M31", "filter": "OSC",
            "time": start.isoformat(), "date_obs": start.isoformat(),
            **good, **card.record_fields(), **kw}


@pytest.fixture
def quiet(monkeypatch):
    monkeypatch.setattr("photonscript.scheduler.runs.sync_goal_progress",
                        lambda c: [])


def test_night_pass_with_the_mount_log(tmp_path, quiet):
    from photonscript.scheduler.runs import _load_subs, append_sub_record
    cfg = _cfg(tmp_path)
    _night_with_a_slew(cfg)
    for i, sec in enumerate((300, 540, 840, 1440)):
        append_sub_record(cfg, NIGHT, _approved(cfg, f"P_{i}.fits", _t(sec)))
    append_sub_record(cfg, NIGHT, _approved(
        cfg, "P_human.fits", _t(560), manual_qa=True, review_source="manual"))
    append_sub_record(cfg, NIGHT, _approved(cfg, "R_0.fits", _t(560), rig="rc16",
                                            exp_s=300.0))
    out = sg.night_pass(cfg, NIGHT)
    assert out["subs"] == 5 and out["judged"] == 5
    assert out["straddled"] == 3 and out["src"] == {"mount-log": 5}
    assert out["newly_rejected"] == 2 and out["split_rate"] == 0.6
    by = {r["file"]: r for r in _load_subs(cfg, NIGHT)}
    assert by["P_0.fits"]["passed_qa"] is True and by["P_0.fits"]["slew_overlap_s"] == 0
    assert by["P_1.fits"]["drivers"] == ["slew_straddle"]      # the slew
    assert by["P_3.fits"]["drivers"] == ["slew_straddle"]      # the flip
    assert "mount log" in by["P_1.fits"]["reason"]
    assert by["P_human.fits"]["passed_qa"] is True               # human kept
    assert "slew_overlap_s" not in by["R_0.fits"]                # RC16 untouched
    again = sg.night_pass(cfg, NIGHT)
    assert again["records_updated"] == 0
    dry = sg.night_pass(cfg, NIGHT, apply=False)
    assert dry["straddled"] == 3 and dry["records_updated"] == 0


def test_night_pass_falls_back_to_rc16_frames(tmp_path, quiet):
    from photonscript.scheduler.runs import _load_subs, append_sub_record
    cfg = _cfg(tmp_path)        # no mount log (a night before PS-67)
    for i, (sec, ra) in enumerate(((0, 10.68), (310, 10.68), (900, 11.53))):
        append_sub_record(cfg, NIGHT, _approved(
            cfg, f"R_{i}.fits", _t(sec), rig="rc16", exp_s=300.0, ra=ra,
            dec=41.27))
    for i, sec in enumerate((100, 560, 1000)):
        append_sub_record(cfg, NIGHT, _approved(cfg, f"P_{i}.fits", _t(sec)))
    out = sg.night_pass(cfg, NIGHT)
    # move between the 610 s end and the 900 s start (+ 30 s settle)
    assert out["src"] == {"rc16-frames": 3} and out["straddled"] == 1
    by = {r["file"]: r for r in _load_subs(cfg, NIGHT)}
    assert by["P_1.fits"]["passed_qa"] is False
    assert "RC16 frames" in by["P_1.fits"]["reason"]
    assert by["P_0.fits"]["passed_qa"] and by["P_2.fits"]["passed_qa"]


def test_rescore_keeps_the_slew_row(tmp_path, quiet):
    from photonscript.scheduler.runs import append_sub_record, rescore_night
    cfg = _cfg(tmp_path)
    append_sub_record(cfg, NIGHT, _approved(cfg, "P_0.fits", _t(0),
                                            slew_overlap_s=40.0,
                                            slew_note="RC16 move"))
    res = rescore_night(cfg, NIGHT)
    assert res["counts"].get("newly_rejected") == 1
    assert res["drivers"] == {"slew_straddle": 1}


# ------------------------------------------- 2026-09-21 M31 replay

def test_replay_2026_09_21_split_subs():
    """M31_OSC3 cull: 92 Piggy subs of 120 s, 47 split_pointing. A synthetic
    mount log with one short move mid-exposure of every split sub (the 51'
    pair, about every 8 min) flags exactly the 47 splits and none of the
    41 keeps, back to back with 0.4 s gaps and the 10 s padding."""
    rows = list(csv.DictReader(FIX.open(encoding="utf-8")))
    assert len(rows) == 92

    def start(r):
        return datetime.fromisoformat(r["date_obs"][:19])
    lines, on_a = [], True
    for r in rows:
        if r["reasons"] != "split_pointing":
            continue
        mid = start(r) + timedelta(seconds=60)
        dec0 = 41.27 if on_a else 41.27 + 51 / 60.0
        lines += [{"dt": mid, "why": "slew-start", "slewing": True,
                   "tracking": True, "ra": 10.68, "dec": dec0},
                  {"dt": mid + timedelta(seconds=8), "why": "slew-end",
                   "slewing": False, "tracking": True, "ra": 10.68,
                   "dec": 41.27 + 51 / 60.0 if on_a else 41.27}]
        on_a = not on_a
    wins = sg.windows_from_mount_log(lines, pad_s=10)
    subs = [{"file": r["file"], "start": start(r),
             "end": start(r) + timedelta(seconds=120)} for r in rows]
    hit = {s["file"] for s in subs if sg.straddles(s["start"], s["end"], wins)}
    splits = {r["file"] for r in rows if r["reasons"] == "split_pointing"}
    keeps = {r["file"] for r in rows if r["action"] == "keep"}
    assert len(splits) == 47 and hit == splits and not hit & keeps


# ------------------------------------------------ live Piggy-600 grade

class _Bus:
    async def publish(self, msg):
        pass


def _agent(cfg, rig):
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config, a.rig, a.bus = cfg, rig, _Bus()
    a.state = TelescopeState()
    a.state.camera_temp_c = 0.2
    a._consecutive_rejects, a._alerted = 0, set()

    async def _esc(*_a, **_k):
        pass
    a._escalate = _esc
    return a


def _light(path: Path, date_obs: str):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = "LIGHT"
    h["EXPTIME"] = 120.0
    h["DATE-OBS"] = date_obs
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((16, 16), np.uint16), header=h).writeto(path)
    return path


@pytest.mark.parametrize("rig,sec,expect", [
    ("piggyback", 540, "fail"),     # exposing through the 600 to 620 s slew
    ("piggyback", 1000, "pass"),    # clear
    ("rc16", 540, "skip"),          # the RC16 owns the mount: not judged
])
def test_live_grade(tmp_path, monkeypatch, rig, sec, expect):
    from photonscript.scheduler.runs import _load_subs
    from photonscript.telescope_agent import agent as agent_mod
    cfg = _cfg(tmp_path)
    _night_with_a_slew(cfg)

    def fake_validate(path, config, pixel_scale=None, rig="rc16"):
        return ImageQualityMetrics(
            hfr_pixels=3.0, fwhm_arcsec=None, star_count=500,
            eccentricity=0.4, background_adu=400.0, noise_adu=10.0,
            clipped_pct=0.0, sat_star_pct=0.0, swamp_factor=9.0,
            exposure_flag="ok")
    monkeypatch.setattr(agent_mod, "validate_image", fake_validate)
    f = _light(tmp_path / "fits" / NIGHT / "LIGHT" / f"{rig}_0001.fits",
               _t(sec).isoformat())
    asyncio.run(_agent(cfg, rig)._process_new_image(f))
    (rec,) = _load_subs(cfg, NIGHT)
    row = next(r for r in rec["scorecard"]["rows"] if r[0] == "slew_straddle")
    assert row[3] == expect
    if expect == "fail":
        assert rec["slew_overlap_s"] == 40.0 and not rec["passed_qa"]
        assert "slew_straddle" in rec["drivers"]
    if rig == "rc16":
        assert "slew_overlap_s" not in rec


def test_config_fields_exposed():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    for env in ("PS_QA_SLEW_STRADDLE_MODE", "PS_SLEW_GATE_PAD_S",
                "PS_SLEW_GATE_MIN_MOVE_ARCMIN"):
        assert hasattr(PhotonScriptConfig(_env_file=None), by_env[env][0])
    c = PhotonScriptConfig(_env_file=None)
    assert c.qa_slew_straddle_mode == "fail" and c.slew_gate_pad_s == 10.0
