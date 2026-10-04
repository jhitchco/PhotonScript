"""PS-67: the pointing record (shared.pointing), the mount log
(shared.mount_log), night events + timeline, the "On target" scorecard check,
the rig-tagged telescope state and the dawn pointing pass."""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest

from photonscript.shared import mount_log, night_events, pointing
from photonscript.shared import qa_rules as q
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.target_names import target_key

NIGHT = "2026-09-26"
CATS = ("Cat's Eye Nebula", 17.97594 * 15, 66.63319)
# the real 2026-09-26 Cat's Eye OIII header values (Library)
CATS_HDR = {"RA": 269.658, "DEC": 66.586, "CENTALT": 32.55, "CENTAZ": 340.1,
            "AIRMASS": 1.86, "PIERSIDE": "East"}
# the PHD2 calibration spot near Dec 0 the 09-26 subs were really shot at
CAL_HDR = {"RA": 269.0, "DEC": 0.4, "CENTALT": 55.0, "PIERSIDE": "West"}


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                stamp_fits_object=False, piggyback_enabled=True)
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


@pytest.fixture(autouse=True)
def _targets(monkeypatch):
    """A fixed coordinate index: no project store, no catalog walk."""
    idx = {target_key(CATS[0]): (*CATS, False),
           target_key("NGC 6543"): (*CATS, False),
           target_key("M31"): ("M31", 10.6847, 41.2690, True)}
    monkeypatch.setattr(pointing, "coord_index", lambda cfg: idx)


@pytest.fixture
def appcfg(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(app, "_store", None)
    return cfg


# ---------------------------------------------------------------- geometry

def test_cats_eye_header_is_on_target(tmp_path):
    cfg = _cfg(tmp_path)
    pos = pointing.from_header(CATS_HDR)
    assert pos["pier"] == "East" and pos["alt"] == 32.55
    a = pointing.assess(cfg, "rc16", pos, "NGC 6543")
    assert a["target"] == CATS[0]
    assert a["off_target_arcmin"] == pytest.approx(2.9, abs=0.1)
    assert a["flag"] == ""


def test_dec0_calibration_spot_named_cats_eye_is_rejected(tmp_path):
    cfg = _cfg(tmp_path)
    a = pointing.assess(cfg, "rc16", pointing.from_header(CAL_HDR), CATS[0])
    assert a["off_target_arcmin"] / 60 == pytest.approx(66, abs=1.5)
    assert a["flag"] == "off-target"
    card = q.evaluate({"pointing_offset_arcmin": a["off_target_arcmin"],
                       "pointing_note": a["note"]}, q.context(cfg, "rc16"))
    row = next(c for c in card.checks if c.id == "pointing")
    assert row.status == q.FAIL and "pointing" in card.drivers
    assert "66 deg" in card.reason and "Cat's Eye Nebula" in card.reason
    assert "pier W" in card.reason and "17h56m" in card.reason


def test_bearing_and_compass():
    assert pointing.bearing(10, 20, 10, 21)[1] == "N"
    assert pointing.bearing(10, 20, 11, 20)[1] == "E"
    assert pointing.bearing(10, 20, 9, 19)[1] == "SW"
    pa, comp = pointing.bearing(10, 0, 10.1, 0.1)
    assert pa == pytest.approx(45, abs=0.5) and comp == "NE"
    assert pointing.bearing(None, 0, 1, 1) == (None, None)


def test_unknown_target_or_position_skips(tmp_path):
    cfg = _cfg(tmp_path)
    assert pointing.assess(cfg, "rc16", pointing.from_header(CATS_HDR),
                           "?")["off_target_arcmin"] is None
    assert pointing.assess(cfg, "rc16", {}, CATS[0])["off_target_arcmin"] is None
    assert pointing.from_header({"OBJECT": "x"}) is None   # Piggy-600 frame


# --------------------------------------------------------- scorecard check

def test_pointing_check_bands_per_rig(tmp_path):
    cfg = _cfg(tmp_path)
    rc, pb = q.thresholds(cfg, "rc16"), q.thresholds(cfg, "piggyback")
    assert (rc["offtarget_flag_arcmin"], rc["offtarget_reject_arcmin"]) == (8, 15)
    assert (pb["offtarget_flag_arcmin"], pb["offtarget_reject_arcmin"]) == (30, 60)
    assert q.pointing_check(4.6, rc).status == q.PASS   # 09-26 container Ha
    assert q.pointing_check(9.0, rc).status == q.WARN
    assert q.pointing_check(16.0, rc).status == q.FAIL
    assert q.pointing_check(16.0, pb).status == q.PASS
    assert q.pointing_check(45.0, pb).status == q.WARN
    assert q.pointing_check(61.0, pb).status == q.FAIL
    assert q.pointing_check(None, rc).status == q.SKIP
    soft = q.thresholds(_cfg(tmp_path, qa_pointing_mode="warn"), "rc16")
    assert q.pointing_check(400.0, soft).status == q.WARN


def test_regrade_pointing_swaps_only_that_row(tmp_path):
    cfg = _cfg(tmp_path)
    t = q.thresholds(cfg, "rc16")
    good = dict(hfr=5.0, ecc=0.4, stars=150, background=400.0, exp_s=300.0,
                ccd_temp=0.2, exposure="ok")
    card = q.evaluate(good, q.context(cfg, "rc16"))
    rec = {"rig": "rc16", **card.record_fields()}
    assert rec["auto_verdict"] == "approved"
    f = q.regrade_pointing(rec, 3960.0, t, "from Cat's Eye Nebula")
    assert f["passed_qa"] is False and f["drivers"] == ["pointing"]
    rows = f["scorecard"]["rows"]
    assert [r[0] for r in rows] == [r[0] for r in rec["scorecard"]["rows"]]
    rec.update(f)
    assert q.regrade_pointing(rec, 3960.0, t, "from Cat's Eye Nebula") is None
    assert q.regrade_pointing({"rig": "rc16"}, 5.0, t) is None   # no card


# --------------------------------------------------------------- mount log

def _mount(ra_h=12.05, dec=0.4, slewing=False, tracking=True, park=False,
           pier="pierWest"):
    return {"RightAscension": ra_h, "Declination": dec, "Altitude": 55.0,
            "Azimuth": 180.0, "SideOfPier": pier, "Slewing": slewing,
            "TrackingEnabled": tracking, "AtPark": park}


def test_mount_logger_change_detection(tmp_path):
    cfg = _cfg(tmp_path, observatory_tz="UTC")
    ml = mount_log.MountLogger(cfg)
    t = datetime(2026, 9, 27, 3, 0, 0)
    first = ml.observe(_mount(), t)
    assert first["why"] == "start" and first["ra"] == pytest.approx(180.75)
    assert ml.observe(_mount(), t + timedelta(seconds=5)) is None
    assert ml.observe(_mount(), t + timedelta(seconds=61))["why"] == "heartbeat"
    assert ml.observe(_mount(slewing=True), t + timedelta(seconds=70))["why"] == "slew-start"
    assert ml.observe(_mount(ra_h=17.97, dec=66.6, slewing=True),
                      t + timedelta(seconds=75))["why"] == "move"
    assert ml.observe(_mount(ra_h=17.97, dec=66.6),
                      t + timedelta(seconds=90))["why"] == "slew-end"
    assert ml.observe(_mount(ra_h=17.97, dec=66.6, pier="pierEast"),
                      t + timedelta(seconds=95))["why"] == "pier"
    assert ml.observe(_mount(ra_h=17.97, dec=66.6, pier="pierEast", tracking=False,
                             park=True), t + timedelta(seconds=99))["why"] == "park"
    assert ml.observe({"Connected": False}, t) is None   # no coordinates
    lines = mount_log.load(cfg, mount_log.log_path(cfg, NIGHT).stem[:10])
    assert [r["why"] for r in lines] == ["start", "heartbeat", "slew-start",
                                         "move", "slew-end", "pier", "park"]
    assert mount_log.log_path(cfg, NIGHT).exists()   # 03:00Z on 09-27 = night of 09-26
    wins = mount_log.slew_windows(lines)
    assert wins == [(t + timedelta(seconds=70), t + timedelta(seconds=90))]
    states = [s["state"] for s in mount_log.segments(lines)]
    assert states == ["tracking", "slewing", "tracking", "parked"]
    pos = mount_log.position_at(lines, t + timedelta(seconds=30))
    assert pos["dec"] == 0.4
    assert mount_log.position_at(lines, t + timedelta(seconds=80)) is None   # slewing
    assert mount_log.moved_during(lines, t, t + timedelta(seconds=100))


def test_piggy_position_from_mount_log(tmp_path):
    cfg = _cfg(tmp_path, observatory_tz="UTC")
    ml = mount_log.MountLogger(cfg)
    t = datetime(2026, 9, 27, 3, 0, 0)
    ml.observe(_mount(ra_h=17.976, dec=66.6, pier="pierEast"), t)
    lines = mount_log.load(cfg, NIGHT)
    rec = pointing.sub_pointing(cfg, "piggyback", {}, t + timedelta(seconds=10),
                                120, CATS[0], mount_lines=lines)
    assert rec["src"] == "mount-log" and rec["pier"] == "East"
    assert rec["off_target_arcmin"] < 5 and rec["flag"] == ""
    assert rec.get("model_err_arcmin") is None   # never for the Piggy-600


# ----------------------------------------------------------------- sidecar

def test_sidecar_last_line_wins(tmp_path):
    cfg = _cfg(tmp_path)
    pointing.append_record(cfg, NIGHT, {"rig": "rc16", "file": "a.fits",
                                        "src": "header", "mount_ra": 1.0})
    pointing.append_record(cfg, NIGHT, {"rig": "rc16", "file": "a.fits",
                                        "src": "solve", "solved_ra": 1.1})
    pointing.append_record(cfg, NIGHT, {"rig": "piggyback", "file": "a.fits",
                                        "src": "mount-log"})
    pts = pointing.load(cfg, NIGHT)
    assert pts[("rc16", "a.fits")]["src"] == "solve"
    assert pts[("piggyback", "a.fits")]["src"] == "mount-log"
    assert pointing.same_record(pts[("rc16", "a.fits")],
                                {"rig": "rc16", "file": "a.fits", "src": "solve",
                                 "solved_ra": 1.1})


def test_model_summary_by_pier_dec_ha():
    recs = [{"rig": "rc16", "mount_ra": 1, "mount_dec": d, "solved_ra": 1,
             "solved_dec": d, "model_err_arcmin": e, "pier": p, "ha_h": h}
            for d, e, p, h in ((10, 4.0, "East", -1.5), (12, 5.0, "East", -0.5),
                               (50, 2.0, "West", 1.0))]
    recs.append({"rig": "piggyback", "flag": "off-target", "mount_ra": 1,
                 "mount_dec": 1})
    s = pointing.summarize(recs)
    assert s["model"]["median_arcmin"] == 4.0 and s["model"]["n"] == 3
    assert s["model"]["by_pier"]["East"] == {"n": 2, "median_arcmin": 4.5}
    assert set(s["model"]["by_dec"]) == {"+0..+30", "+30..+60"}
    assert s["off_target"] == 1 and s["rigs"]["piggyback"]["off_target"] == 1


# ------------------------------------------------------- rig-tagged state

def test_piggyback_broadcast_does_not_overwrite_rc16_state(monkeypatch):
    import photonscript.scheduler.app as app
    from photonscript.shared.models import AgentMessage, AgentRole, TelescopeState
    monkeypatch.setattr(app, "_telescope_state", TelescopeState())
    monkeypatch.setattr(app, "_rig_states", {})
    monkeypatch.setattr(app, "_ws_clients", [])

    async def run():
        for payload in ({"rig": "rc16", "mount_ra": 12.05, "mount_dec": 0.4},
                        {"rig": "piggyback", "mount_ra": 0.0, "mount_dec": 0.0,
                         "camera_temp_c": 0.1}):
            await app.on_agent_message(AgentMessage(
                sender=AgentRole.TELESCOPE, recipient=AgentRole.SCHEDULER,
                msg_type="telescope_state_update", payload=payload))
        return await app.api_status()
    st = asyncio.run(run())
    assert st["telescope"]["mount_ra"] == 12.05
    assert st["rigs"]["piggyback"]["camera_temp_c"] == 0.1
    assert st["rigs"]["rc16"]["mount_dec"] == 0.4


class _FakeNina:
    def __init__(self):
        self.calls = []

    async def get_camera_info(self):
        self.calls.append("camera")
        return {"Temperature": 0.1, "CoolerOn": True}

    async def get_mount_info(self):
        self.calls.append("mount")
        return _mount()

    async def get_focuser_info(self):
        return {"Position": 1}

    async def get_filter_wheel_info(self):
        return {}

    async def get_sequence_status(self):
        return {"State": "RUNNING", "Running": "Smart Exposure"}


@pytest.mark.parametrize("rig", ["rc16", "piggyback"])
def test_poll_loop_reads_the_mount_only_on_the_rig_that_owns_it(tmp_path, monkeypatch, rig):
    from photonscript.telescope_agent import agent as agent_mod
    cfg = _cfg(tmp_path, observatory_tz="UTC")
    ag = agent_mod.TelescopeAgent(cfg, rig=rig)
    ag.nina = _FakeNina()
    for name in ("_cooling_watchdog", "_dew_heater_watchdog"):
        async def _noop(*a, **k):
            return None
        monkeypatch.setattr(ag, name, _noop)

    async def _stop(_s):
        ag._running = False
    monkeypatch.setattr(agent_mod.asyncio, "sleep", _stop)
    ag._running = True
    asyncio.run(ag._nina_poll_loop())
    assert ("mount" in ag.nina.calls) == (rig == "rc16")
    assert ag.state.rig == rig
    runs = Path(cfg.data_dir) / "runs"
    mounts = list(runs.glob("*_mount.jsonl"))
    assert bool(mounts) == (rig == "rc16")
    ev = [json.loads(x) for p in runs.glob("*_events.jsonl")
          for x in p.read_text().splitlines()]
    assert ev and ev[0]["kind"] == "instruction" and ev[0]["value"] == "Smart Exposure"
    assert ev[0]["rig"] == rig


def test_sequence_status_reports_the_running_leaf():
    from photonscript.telescope_agent.nina_client import _sequence_status
    tree = [{"Name": "Targets_Container", "Status": "RUNNING", "Items": [
        {"Name": "M31_Container", "Status": "RUNNING", "Items": [
            {"Name": "Slew and center", "Status": "FINISHED"},
            {"Name": "Run Autofocus", "Status": "RUNNING"}]}]}]
    out = _sequence_status(tree)
    assert out["Running"] == "Run Autofocus"
    assert out["CurrentTarget"]["Name"] == "M31_Container"
    assert "Running" not in _sequence_status([])


# ------------------------------------------------------ night timeline

def test_timeline_merges_mount_events_and_subs(tmp_path):
    from photonscript.scheduler.night_timeline import classify_instruction, timeline
    from photonscript.scheduler.runs import append_sub_record
    cfg = _cfg(tmp_path, observatory_tz="UTC")
    assert classify_instruction("Slew and center") == "centering"
    assert classify_instruction("Run Autofocus") == "autofocus"
    assert classify_instruction("Smart Exposure") == "exposing"
    assert classify_instruction("Meridian Flip") == "meridian flip"
    assert classify_instruction("") is None
    t = datetime(2026, 9, 27, 3, 0, 0)
    ml = mount_log.MountLogger(cfg)
    ml.observe(_mount(slewing=True), t)
    ml.observe(_mount(), t + timedelta(seconds=40))
    ev = night_events.EventLog(cfg, "rc16")
    ev.change("nina", "instruction", "Slew and center", now=t)
    ev.change("nina", "instruction", "Smart Exposure", now=t + timedelta(seconds=90))
    ev.change("phd2", "guider", "guiding", now=t + timedelta(seconds=80))
    ev.rms(0.42, "arcsec", now=t + timedelta(seconds=100))
    assert ev.rms(0.5, "arcsec", now=t + timedelta(seconds=110)) is None  # 60 s rate
    assert ev.change("phd2", "guider", "guiding") is None                 # no change
    append_sub_record(cfg, NIGHT, {"rig": "rc16", "file": "L/a.fits",
                                   "time": "2026-09-27T03:01:30", "exp_s": 300,
                                   "target": "M31", "filter": "L",
                                   "passed_qa": True})
    tl = timeline(cfg, NIGHT)
    rows = {r["id"]: r for r in tl["rows"]}
    assert [s["state"] for s in rows["mount"]["segments"]] == ["slewing", "tracking"]
    assert [s["state"] for s in rows["rc16"]["segments"]] == ["centering", "exposing"]
    g = rows["guider"]["segments"][0]
    assert g["state"] == "guiding" and g["label"] == 'RMS 0.42"'
    assert tl["subs"]["rc16"][0]["verdict"] == "review"
    assert timeline(cfg, "2026-01-01")["ok"] is False


# ------------------------------------------------------ the dawn pass

def _fits(path: Path, hdr: dict):
    from astropy.io import fits
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    h["IMAGETYP"] = "LIGHT"
    for k, v in hdr.items():
        h[k] = v
    fits.PrimaryHDU(np.zeros((8, 8), np.uint16), header=h).writeto(path, overwrite=True)
    return path


def _approved_record(cfg, name, target, rig="rc16", **kw):
    good = dict(hfr=5.0, ecc=0.4, stars=150, background=400.0, exp_s=300.0,
                ccd_temp=0.2, exposure="ok")
    card = q.evaluate(good, q.context(cfg, rig))
    return {"rig": rig, "file": name, "target": target, "filter": "OIII",
            "exp_s": 300.0, **good, **card.record_fields(), **kw}


def test_night_pass_rejects_the_calibration_spot_subs(tmp_path, appcfg, monkeypatch):
    from photonscript.scheduler import pointing_record
    from photonscript.scheduler.runs import _load_subs, append_sub_record
    cfg = appcfg
    monkeypatch.setattr("photonscript.scheduler.runs.sync_goal_progress",
                        lambda c: [])
    fits_dir = Path(cfg.image_watch_dir) / NIGHT
    recs = []
    for i in range(4):   # 04:00Z on: good Cat's Eye subs, then the cal spot
        hdr = CATS_HDR if i < 2 else CAL_HDR
        f = _fits(fits_dir / f"L_{i}.fits", hdr)
        recs.append(_approved_record(
            cfg, f"L_{i}.fits", CATS[0], abs_path=str(f),
            time=f"2026-09-27T04:{i * 6:02d}:00"))
    recs.append(_approved_record(cfg, "L_human.fits", CATS[0],
                                 abs_path=str(_fits(fits_dir / "L_h.fits", CAL_HDR)),
                                 time="2026-09-27T04:30:00", manual_qa=True,
                                 review_source="manual"))
    for r in recs:
        append_sub_record(cfg, NIGHT, r)
    out = pointing_record.night_pass(cfg, NIGHT, solve=False)
    assert out["verdicts_changed"] == 2 and out["newly_rejected"] == 2
    by = {r["file"]: r for r in _load_subs(cfg, NIGHT)}
    assert by["L_0.fits"]["passed_qa"] is True
    assert by["L_0.fits"]["pointing_offset_arcmin"] == pytest.approx(2.9, abs=0.1)
    assert by["L_2.fits"]["passed_qa"] is False
    assert by["L_2.fits"]["drivers"] == ["pointing"]
    assert "reviewed" not in by["L_2.fits"] or not by["L_2.fits"]["reviewed"]
    assert by["L_human.fits"]["passed_qa"] is True        # human verdict kept
    pts = pointing.load(cfg, NIGHT)
    assert pts[("rc16", "L_2.fits")]["flag"] == "off-target"
    assert out["summary"]["off_target"] == 3
    # second run: nothing new to write or change
    again = pointing_record.night_pass(cfg, NIGHT, solve=False)
    assert again["written"] == 0 and again["records_updated"] == 0
    # API: merged into the night detail and served
    from photonscript.scheduler.routers import pointing as prouter
    d = {"subs": [dict(r) for r in _load_subs(cfg, NIGHT)]}
    prouter.merge_night(cfg, NIGHT, d)
    s2 = next(s for s in d["subs"] if s["file"] == "L_2.fits")
    assert s2["pointing"]["flag"] == "off-target" and d["pointing"]["off_target"] == 3
    assert prouter.api_pointing(NIGHT)["summary"]["off_target"] == 3


def test_night_pass_sampled_solves_and_model_error(tmp_path, appcfg, monkeypatch):
    from photonscript.scheduler import pointing_record, solve_store
    from photonscript.scheduler.runs import append_sub_record
    cfg = appcfg
    monkeypatch.setattr("photonscript.scheduler.runs.sync_goal_progress",
                        lambda c: [])
    fits_dir = Path(cfg.image_watch_dir) / NIGHT
    for i in range(12):
        f = _fits(fits_dir / f"L_{i:02d}.fits", CATS_HDR)
        append_sub_record(cfg, NIGHT, _approved_record(
            cfg, f"L_{i:02d}.fits", CATS[0], abs_path=str(f),
            time=f"2026-09-27T04:{i * 5:02d}:00"))
    calls = []

    def runner(path, fov, hint, radius, timeout):
        calls.append((Path(path).name, hint, radius))
        # the true center sits 4' south of where the mount says it is
        return {"CRVAL1": str(hint[0]), "CRVAL2": str(hint[1] - 4 / 60.0),
                "CD1_1": "-6.6e-5", "CD1_2": "0", "CD2_1": "0", "CD2_2": "6.6e-5"}
    out = pointing_record.night_pass(cfg, NIGHT, solve=True, runner=runner)
    # sampled: every 10th (0, 10) plus the first sub (no mount log: first
    # after a slew = the night's first sub)
    assert sorted(c[0] for c in calls) == ["L_00.fits", "L_10.fits"]
    assert all(c[2] == 5.0 for c in calls)    # hinted radius
    assert out["solved"] == 2
    assert len(solve_store.load(cfg, NIGHT, "rc16")) == 2
    pts = pointing.load(cfg, NIGHT)
    p = pts[("rc16", "L_00.fits")]
    assert p["src"] == "solve" and p["mount_src"] == "header"
    assert p["model_err_arcmin"] == pytest.approx(4.0, abs=0.05)
    assert p["model_err_pa"] == pytest.approx(180, abs=1)
    assert out["summary"]["model"]["median_arcmin"] == pytest.approx(4.0, abs=0.05)
    # stored attempts are never re-run
    calls.clear()
    pointing_record.night_pass(cfg, NIGHT, solve=True, runner=runner)
    assert calls == []


def test_pick_solves_order_and_policy():
    from photonscript.scheduler.pointing_record import pick_solves
    t = datetime(2026, 9, 27, 4)
    frames = [{"rec": {"file": f"f{i}", "target": "A" if i < 5 else "B"},
               "start": t + timedelta(minutes=5 * i),
               "point": {"mount_ra": 10 if i < 5 else 50, "mount_dec": 20,
                         "flag": "flag" if i == 7 else ""}}
              for i in range(12)]
    got = [f["rec"]["file"] for f in pick_solves(frames, "sampled", 10)]
    assert got == ["f7", "f0", "f5", "f10"]
    assert pick_solves(frames, "off", 10) == []
    assert len(pick_solves(frames, "all", 10, done={"f1"})) == 11
    wins = [(t + timedelta(minutes=21), t + timedelta(minutes=23))]
    got = [f["rec"]["file"] for f in pick_solves(frames, "sampled", 100, wins)]
    assert got == ["f7", "f0", "f5"]


def test_rescore_reads_the_pointing_sidecar(tmp_path, appcfg):
    from photonscript.scheduler.runs import append_sub_record, rescore_night
    cfg = appcfg
    append_sub_record(cfg, NIGHT, _approved_record(cfg, "a.fits", CATS[0],
                                                   time="2026-09-27T04:00:00"))
    pointing.append_record(cfg, NIGHT, {"rig": "rc16", "file": "a.fits",
                                        "off_target_arcmin": 3960.0,
                                        "note": "from Cat's Eye Nebula"})
    res = rescore_night(cfg, NIGHT)
    assert res["counts"].get("newly_rejected") == 1
    assert res["drivers"] == {"pointing": 1}
