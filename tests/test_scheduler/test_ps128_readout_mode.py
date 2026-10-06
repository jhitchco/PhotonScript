"""PS-128: darks and bias match the lights' readout mode (HCG vs LCG), not
just exposure / gain / offset / set temp. The RC16 AP26MC has shot HCG lights
since 2026-09-26 (READOUTM "High Conversion Gain") while its 600 s darks and
its bias in the Library are LCG: the owed view, the night quota (RC16 armer,
Piggy-600 companion) and readiness must stop counting them."""

import asyncio
from datetime import datetime

from astropy.io import fits

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import calibration_capture as cc
from photonscript.scheduler import calibration_owed as co
from photonscript.scheduler import calibration_qa as cq
from photonscript.scheduler import runs
from photonscript.scheduler import sequence_lint as sl
from photonscript.shared import rigs
from photonscript.shared.config import PhotonScriptConfig
from tests.test_scheduler.test_ps113_calibration import write_frame
from tests.test_scheduler.test_ps122_calibration_owed import (_ago, _cfg, _heart,
                                                             _m31, _pb_cfg, osc,
                                                             store_frames, subs_log)

HCG, LCG = "High Conversion Gain", "Low Conversion Gain"


def frame(path, readout=None, **kw):
    """A synthetic calibration FITS with an optional READOUTM keyword."""
    write_frame(path, **kw)
    if readout is not None:
        fits.setval(path, "READOUTM", value=readout)
    return path


def _by(r):
    return {(d["exp_s"], d["readout"]): d for d in r["darks"]}


# --- header / config helpers -------------------------------------------------

def test_normalize_and_header_readout():
    n = rigs.normalize_readout
    assert n(HCG) == "HCG" and n(" high  conversion gain ") == "HCG"
    assert n(LCG) == "LCG" and n("lcg") == "LCG"
    assert n("HDR") == "HDR" and n("") is None and n(None) is None
    assert rigs.header_readout({"READOUTM": HCG}) == ("HCG", HCG)
    assert rigs.header_readout({"READMODE": "LCG"}) == ("LCG", "LCG")
    assert rigs.header_readout({"GAIN": 200}) == (None, None)
    # the subs log keeps the raw header text (PS-122 shape)
    assert rigs.light_epoch_fields({"GAIN": 200, "READOUTM": HCG})["readout"] == HCG


def test_rig_readout_defaults_and_blank(tmp_path):
    c = PhotonScriptConfig(_env_file=None)
    assert c.camera_readout_mode == "HCG" and c.piggyback_readout_mode == "LCG"
    assert rigs.rig_readout(c, "rc16") == "HCG"
    assert rigs.rig_readout(c, "piggyback") == "LCG"
    # the piggyback view carries its own mode for the header scan
    assert rigs.rig_config(c, "piggyback").camera_readout_mode == "LCG"
    off = _cfg(tmp_path, camera_readout_mode="")
    assert rigs.rig_readout(off, "rc16") is None
    assert cal.dark_epoch(c, "rc16")["readout"] == "HCG"
    assert cal.dark_epoch(c, "piggyback")["readout"] == "LCG"


def test_config_fields_on_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    assert by_env["PS_CAMERA_READOUT_MODE"][0] == "camera_readout_mode"
    assert by_env["PS_PIGGYBACK_READOUT_MODE"][0] == "piggyback_readout_mode"
    assert by_env["PS_CAMERA_READOUT_MODE"][4] == "str"


def test_camera_info_readout():
    info = {"ReadoutModes": ["Low Conversion Gain", "High Conversion Gain"],
            "ReadoutMode": 0, "ReadoutModeForNormalImages": 1}
    assert rigs.camera_info_readout(info) == ("HCG", HCG)
    assert rigs.camera_info_readout({"ReadoutModes": {"$values": [LCG, HCG]},
                                     "ReadoutMode": 0}) == ("LCG", LCG)
    assert rigs.camera_info_readout({"Temperature": 0.1}) == (None, None)
    assert rigs.camera_info_readout(None) == (None, None)


# --- QA records ----------------------------------------------------------------

def test_qa_records_store_readout(tmp_path):
    cfg = _cfg(tmp_path)
    lib = runs.library_root(cfg)
    d = _ago(2)
    frames = [("DARK", d, frame(lib / "Calibration" / "DARK" / d / f"h{i}.fits",
                                HCG, exp=600.0, gain=200, instrume="AP26MC", seed=i))
              for i in range(2)]
    frames.append(("DARK", d, frame(lib / "Calibration" / "DARK" / d / "n.fits",
                                    None, exp=600.0, gain=200, instrume="AP26MC",
                                    seed=9)))
    recs = cq.qa_frames(cfg, "rc16", frames)
    ros = sorted((r["name"], r["readout"], r["readout_raw"]) for r in recs.values())
    assert ros == [("h0.fits", "HCG", HCG), ("h1.fits", "HCG", HCG),
                   ("n.fits", None, None)]
    n = recs[cq.frame_key("DARK", d, "n.fits")]
    assert cq.frame_readout(n, "HCG") == ("HCG", True)       # assumed
    assert cq.frame_readout(recs[cq.frame_key("DARK", d, "h0.fits")], "LCG") \
        == ("HCG", False)


def test_backfill_fills_readout_without_remeasuring(tmp_path, monkeypatch):
    """Records measured before PS-128 get the readout from a header-only read
    on the next pass; nothing is re-measured and nothing moves."""
    cfg = _cfg(tmp_path)
    lib = runs.library_root(cfg)
    d = _ago(3)
    paths = [frame(lib / "Calibration" / "BIAS" / d / f"b{i}.fits", LCG, typ="BIAS",
                   exp=0.001, gain=200, level=256.0, instrume="AP26MC", seed=i)
             for i in range(3)]
    cq.qa_frames(cfg, "rc16", [("BIAS", d, p) for p in paths])
    store = cq.load_store(cfg, "rc16")
    for r in store["frames"].values():          # what a pre-PS-128 store holds
        r.pop("readout", None)
        r.pop("readout_raw", None)
    cq.save_store(cfg, "rc16", store)

    def boom(*a, **k):
        raise AssertionError("re-measured")

    monkeypatch.setattr(cq, "measure", boom)
    rep = cq.backfill(cfg, "rc16", dry_run=True)
    assert rep["rigs"]["rc16"]["would_quarantine"] == []
    recs = cq.load_store(cfg, "rc16")["frames"]
    assert {r["readout"] for r in recs.values()} == {"LCG"}
    assert all(p.exists() for p in paths)


def test_capture_expect_readout_header_fail():
    rec = {"type": "DARK", "exptime": 600.0, "gain": 200, "offset": 256,
           "ccdtemp": 0.0, "settemp": 0.0, "readout": "LCG", "readout_raw": LCG,
           "expect": {"gain": 200, "offset": 256, "readout": "HCG"}}
    fails, _w = cq.judge_frame(rec, rig="rc16")
    assert any(f["code"] == "header" and "READOUTM" in f["detail"] for f in fails)
    rec["readout"] = "HCG"
    fails, _w = cq.judge_frame(rec, rig="rc16")
    assert not any("READOUTM" in f["detail"] for f in fails)


# --- counting ------------------------------------------------------------------

def _rc16_store(cfg):
    """The RC16 Library as PS-117 found it: LCG 600 s darks and LCG bias,
    HCG 180 s darks."""
    store_frames(cfg, "rc16", [
        ("DARK", _ago(40), 7, {"exptime": 600.0, "readout": "LCG"}),
        ("DARK", _ago(9), 29, {"exptime": 180.0, "readout": "HCG"}),
        ("BIAS", _ago(40), 50, {"exptime": 0.001, "readout": "LCG"}),
    ])


def test_quota_counts_only_the_lights_readout(tmp_path):
    cfg = _cfg(tmp_path)
    _rc16_store(cfg)
    assert cal.dark_quota(cfg, "rc16", 600.0)["have"] == 0
    assert cal.dark_quota(cfg, "rc16", 600.0, readout="LCG")["have"] == 7
    assert cal.dark_quota(cfg, "rc16", 180.0)["have"] == 29
    assert cq.count_passed_bias(cfg, "rc16", gain=200, offset=256) == 0
    assert cq.count_passed_bias(cfg, "rc16", gain=200, offset=256, readout="LCG") == 50
    # blank camera_readout_mode: the pre-PS-128 count (readout ignored)
    legacy = _cfg(tmp_path, camera_readout_mode="")
    assert cal.dark_quota(legacy, "rc16", 600.0)["have"] == 7
    assert cq.count_passed_bias(legacy, "rc16", gain=200, offset=256) == 50


def test_frames_without_readout_are_assumed_the_rigs(tmp_path):
    cfg = _cfg(tmp_path)
    store_frames(cfg, "rc16", [("DARK", _ago(2), 4, {"exptime": 600.0})])
    assert cal.dark_quota(cfg, "rc16", 600.0)["have"] == 4          # assumed HCG
    assert cal.dark_quota(cfg, "rc16", 600.0, readout="LCG")["have"] == 0
    lcg = _cfg(tmp_path, camera_readout_mode="LCG")
    assert cal.dark_quota(lcg, "rc16", 600.0)["have"] == 4          # assumed LCG


def test_header_scan_matches_readout(tmp_path):
    cfg = _cfg(tmp_path, calibration_qa_mode="off")
    lib = runs.library_root(cfg)
    d = _ago(2)
    for i in range(3):
        frame(lib / "Calibration" / "DARK" / d / f"l{i}.fits", LCG, exp=600.0,
              gain=200, seed=i)
    for i in range(2):
        frame(lib / "Calibration" / "DARK" / d / f"h{i}.fits", HCG, exp=600.0,
              gain=200, seed=10 + i)
    frame(lib / "Calibration" / "DARK" / d / "none.fits", None, exp=600.0, gain=200,
          seed=20)
    assert cal.count_matching_darks(cfg, 600.0) == 3                 # 2 HCG + 1 assumed
    assert cal.count_matching_darks(cfg, 600.0, readout="LCG") == 3
    assert cal.darks_have(cfg, "rc16", 600.0) == 3
    off = _cfg(tmp_path, calibration_qa_mode="off", camera_readout_mode="")
    assert cal.count_matching_darks(off, 600.0) == 6


def test_rc16_armer_unsafe_darks_fill_hcg(tmp_path, monkeypatch):
    from photonscript.scheduler import nina_sequence_json as nsj
    cfg = _cfg(tmp_path)
    _rc16_store(cfg)
    monkeypatch.setattr(nsj, "_gen_cfg", lambda: cfg)
    names = {b["Name"] for b in nsj._dark_quota_blocks("DawnProvider", 0)}
    assert "DARKS_600s (need 30 of 30)" in names        # the LCG darks don't count
    assert "DARKS_180s (need 1 of 30)" in names


def test_companion_counts_at_the_piggyback_readout(tmp_path):
    from photonscript.scheduler.calibration import _osc_dark_blocks
    cfg = _pb_cfg(tmp_path, piggyback_dark_exposures="120")
    store_frames(cfg, "piggyback", [
        ("DARK", _ago(5), 10, {"exptime": 120.0, "readout": "LCG"}),
        ("DARK", _ago(6), 6, {"exptime": 120.0, "readout": "HCG"})])
    names = [b["Name"] for b in _osc_dark_blocks(cq.rig_view(cfg, "piggyback"))]
    assert names == ["OSC DARKS_120s (need 20 of 30)"]
    hcg = _pb_cfg(tmp_path, piggyback_dark_exposures="120",
                  piggyback_readout_mode="HCG")
    names = [b["Name"] for b in _osc_dark_blocks(cq.rig_view(hcg, "piggyback"))]
    assert names == ["OSC DARKS_120s (need 24 of 30)"]


def test_bias_age_ignores_other_readout(tmp_path):
    cfg = _cfg(tmp_path)
    lib = runs.library_root(cfg)
    frame(lib / "Calibration" / "BIAS" / _ago(10) / "b.fits", LCG, typ="BIAS",
          exp=0.001, gain=200)
    assert cal.days_since_last_bias(cfg) is None                    # no HCG bias
    assert cal.days_since_last_bias(_cfg(tmp_path, camera_readout_mode="LCG")) == 10
    frame(lib / "Calibration" / "BIAS" / _ago(30) / "b.fits", HCG, typ="BIAS",
          exp=0.001, gain=200)
    assert cal.days_since_last_bias(cfg) == 30
    frame(lib / "Calibration" / "BIAS" / _ago(4) / "b.fits", None, typ="BIAS",
          exp=0.001, gain=200)
    assert cal.days_since_last_bias(cfg) == 4                       # assumed HCG


# --- the owed view -------------------------------------------------------------

def test_owed_shows_hcg_600s_darks_and_hcg_bias(tmp_path):
    cfg = _cfg(tmp_path)
    _rc16_store(cfg)
    heart = {"rig": "rc16", "target": "Heart Nebula", "filter": "Ha", "exp_s": 600.0,
             "gain": 200, "offset": 256, "set_temp": 0.0, "xbin": 1}
    n_old, n_new, n_pre = _ago(45), _ago(3), _ago(2)
    subs_log(cfg, n_old, [dict(heart, readout=LCG)])
    subs_log(cfg, n_new, [dict(heart, readout=HCG), dict(heart, readout=HCG)])
    subs_log(cfg, n_pre, [{k: v for k, v in heart.items() if k != "gain"}])
    r = co.owed_report(cfg, "rc16", projects=[_heart()])["rigs"][0]
    assert r["epoch"]["readout"] == "HCG"
    d = _by(r)
    hcg = d[(600.0, "HCG")]
    assert hcg["have"] == 0 and hcg["owed"] == 30 and hcg["on_epoch"]
    assert hcg["lights"] == 3 and hcg["lights_readout_assumed"] == 1
    assert "HCG" in hcg["label"] and hcg["auto_fill"]
    lcg = d[(600.0, "LCG")]
    assert lcg["have"] == 7 and lcg["lights"] == 1 and not lcg["on_epoch"]
    assert "LCG" in lcg["fix"] and "HCG" in lcg["fix"]
    assert d[(180.0, "HCG")]["have"] == 29
    assert r["bias"]["owed"] and r["bias"]["reasons"] == ["no bias"]
    assert r["bias"]["label"] == "bias (HCG)"
    assert r["lights_readout_assumed"] == 1
    assert any(t.startswith("Bias (HCG)") for t in r["items"])
    assert any("600 s (gain 200, offset 256, 0 C, HCG): 0 of 30" in t
               for t in r["items"])
    assert "HCG" in co.format_report({"generated": "x", "lookback_days": 60,
                                      "qa_mode": "quarantine", "rigs": [r]})


def test_owed_flags_assumed_frames(tmp_path):
    cfg = _cfg(tmp_path)
    store_frames(cfg, "rc16", [("DARK", _ago(2), 5, {"exptime": 600.0}),
                               ("BIAS", _ago(2), 50, {"exptime": 0.001})])
    r = co.owed_report(cfg, "rc16", projects=[])["rigs"][0]
    assert r["frames_readout_assumed"] == 55
    assert _by(r)[(600.0, "HCG")]["have"] == 5
    assert "Readout assumed HCG for 55" in r["readout_note"]
    assert not any("Readout assumed" in t for t in r["items"])   # not an owed item


def test_owed_readout_blank_is_pre_ps128(tmp_path):
    cfg = _cfg(tmp_path, camera_readout_mode="")
    _rc16_store(cfg)
    subs_log(cfg, _ago(3), [{"rig": "rc16", "target": "Heart Nebula", "filter": "Ha",
                             "exp_s": 600.0, "gain": 200, "offset": 256,
                             "set_temp": 0.0, "xbin": 1, "readout": HCG}])
    r = co.owed_report(cfg, "rc16", projects=[_heart()])["rigs"][0]
    d = _by(r)
    assert all(ro is None for _e, ro in d)
    assert d[(600.0, None)]["have"] == 7 and d[(600.0, None)]["owed"] == 23
    assert not r["bias"]["owed"] and r["frames_readout_assumed"] == 0


def test_piggy_owed_unchanged_at_lcg(tmp_path):
    cfg = _pb_cfg(tmp_path)
    store_frames(cfg, "piggyback", [("DARK", _ago(5), 30,
                                     {"exptime": 120.0, "readout": "LCG"})])
    subs_log(cfg, _ago(2), [osc(120.0, readout=LCG)])
    r = co.owed_report(cfg, "piggyback", projects=[_m31()])["rigs"][0]
    assert _by(r)[(120.0, "LCG")]["owed"] == 0


# --- NINA: capture preflight and the lint ----------------------------------------

class _IO:
    def __init__(self, info):
        self.info = info

    async def connect_camera(self):
        return True, ""

    async def camera(self):
        return self.info

    async def sequence_running(self):
        return False

    async def roof(self, connect=True):
        return True, "fake"

    async def guider_state(self):
        return ""


def _preflight(cfg, info):
    return asyncio.run(cc.preflight(cfg, "rc16", "DISARMED", io=_IO(info),
                                    daytime=False))


def test_capture_refused_when_nina_readout_differs(tmp_path):
    cfg = _cfg(tmp_path)
    lcg = {"Connected": True, "ReadoutModes": [LCG, HCG],
           "ReadoutModeForNormalImages": 0}
    refusals, seen = _preflight(cfg, lcg)
    assert any("readout mode" in x and "HCG" in x for x in refusals)
    assert seen["readout"] == {"nina": LCG, "want": "HCG"}
    hcg = dict(lcg, ReadoutModeForNormalImages=1)
    refusals, _ = _preflight(cfg, hcg)
    assert not any("readout" in x for x in refusals)
    refusals, _ = _preflight(cfg, {"Connected": True})          # unknown: fail open
    assert not any("readout" in x for x in refusals)
    refusals, _ = _preflight(_cfg(tmp_path, camera_readout_mode=""), lcg)
    assert not any("readout" in x for x in refusals)


def test_capture_job_expects_the_readout(tmp_path):
    from photonscript.scheduler.calibration_plan import rig_epoch
    cfg = _cfg(tmp_path)
    assert rig_epoch(cfg, "rc16")["readout"] == "HCG"
    assert rig_epoch(_pb_cfg(tmp_path), "piggyback")["readout"] == "LCG"


def _exp(kind, i):
    return {"$id": str(i), "$type": "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, "
            "NINA.Sequencer", "ImageType": kind}


def _ro(mode, i):
    return {"$id": str(i), "$type": "NINA.Sequencer.SequenceItem.Camera.SetReadoutMode, "
            "NINA.Sequencer", "Mode": mode}


def _seq(*items):
    return {"$id": "0", "$type": "Root", "Items": {"$values": list(items)}}


def _readout_findings(seq):
    r = sl.LintResult()
    sl._check_readout_mode(seq, r)
    return [f for f in r.findings if f.rule == "readout"]


def test_lint_readout_mode():
    # no Set readout mode: NINA's profile mode for everything, nothing to say
    assert _readout_findings(_seq(_exp("LIGHT", 1), _exp("DARK", 2))) == []
    # the same mode for lights and darks
    assert _readout_findings(_seq(_ro(1, 1), _exp("LIGHT", 2), _exp("DARK", 3))) == []
    # darks switched to another mode
    bad = _readout_findings(_seq(_ro(1, 1), _exp("LIGHT", 2), _ro(0, 3),
                                 _exp("DARK", 4)))
    assert len(bad) == 1 and bad[0].level == "ERROR"
    # lights at the profile mode, bias after an explicit mode
    assert _readout_findings(_seq(_exp("LIGHT", 1), _ro(0, 2), _exp("BIAS", 3)))
    # flats are not checked
    assert _readout_findings(_seq(_ro(1, 1), _exp("LIGHT", 2), _ro(0, 3),
                                  _exp("FLAT", 4))) == []


def test_generated_calibration_sequence_has_no_readout_finding(tmp_path):
    import json
    cfg = _cfg(tmp_path)
    text, _m = cal.generate_darks_json(cfg, [(600.0, 2)], bias_count=5)
    assert _readout_findings(json.loads(text)) == []
    assert "SetReadoutMode" not in text      # NINA's profile mode applies


def test_now_is_used(tmp_path):
    # guard: the fixtures date frames relative to today
    assert _ago(0) == datetime.now().strftime("%Y-%m-%d")
