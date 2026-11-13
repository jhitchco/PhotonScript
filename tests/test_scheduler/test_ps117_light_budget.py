"""PS-117 part (b): the light budget. The noise model, sub-length advice and
SNR progress (shared.exposure_analysis) with the PS-117 grooming numbers as
fixtures; the sky fields both graders record; the qa-rescore backfill; the
light-budget API; the M31 goal decision; and exposure-report / --camera-cal
on synthetic Library frames."""

import json
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import runs
from photonscript.shared import exposure_analysis as ea
from photonscript.shared import rigs
from photonscript.shared import star_measure as sm
from photonscript.shared.config import PhotonScriptConfig

from tests.test_scheduler.test_ps83_measure_parity import (NIGHT, _field,
                                                          _live, _need_sep,
                                                          _write)
from tests.test_scheduler.test_ps83_measure_parity import _cfg as _pcfg

LCG = {"READOUTM": "Low Conversion Gain"}


def _piggy():
    return ea.CameraModel.from_config(PhotonScriptConfig(_env_file=None),
                                      "piggyback", readout="LCG")


# --- config ---------------------------------------------------------------------

def test_new_config_keys_and_the_piggyback_view():
    c = PhotonScriptConfig(_env_file=None)
    assert (c.camera_bias_adu, c.camera_dark_e_s) == (256.0, 0.0)
    assert (c.piggyback_bias_adu, c.piggyback_dark_e_s) == (256.5, 0.023)
    assert c.light_budget_goal_snr == 20.0
    pv = rigs.rig_config(c, rigs.PIGGYBACK)
    assert (pv.camera_bias_adu, pv.camera_dark_e_s) == (256.5, 0.023)
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_attr = {f[0]: f for f in _CONFIG_FIELDS}
    for attr, env in (("camera_bias_adu", "PS_CAMERA_BIAS_ADU"),
                      ("camera_dark_e_s", "PS_CAMERA_DARK_E_S"),
                      ("light_budget_goal_snr", "PS_LIGHT_BUDGET_GOAL_SNR"),
                      ("piggyback_bias_adu", "PS_PIGGYBACK_BIAS_ADU"),
                      ("piggyback_dark_e_s", "PS_PIGGYBACK_DARK_E_S")):
        assert by_attr[attr][1] == env and by_attr[attr][4] == "float"


def test_camera_model_per_rig_and_mode():
    c = PhotonScriptConfig(_env_file=None)
    p = _piggy()
    assert (p.gain_e_adu, p.read_noise_adu, p.osc) == (0.74, 3.27, True)
    assert p.read_noise_e == pytest.approx(2.42, abs=0.01)
    hcg = ea.CameraModel.from_config(c, "rc16")
    assert (hcg.gain_e_adu, hcg.read_noise_adu, hcg.readout) == (0.25, 5.66,
                                                                 "HCG")
    lcg = ea.CameraModel.from_config(c, "rc16", readout="Low Conversion Gain")
    assert (lcg.gain_e_adu, lcg.read_noise_adu) == (0.79, 4.27)
    assert ea.CameraModel.from_config(c, "rc16", readout="LCG").readout == "LCG"


# --- the noise model with the grooming numbers -------------------------------------

@pytest.mark.parametrize("sky,t5,t10", [
    (0.339, 158, 77),     # G, dark 09-21 sky
    (0.46, 118, 58),      # G, 10-03 evening sky
])
def test_sky_limited_lengths(sky, t5, t10):
    cam = _piggy()
    assert ea.sky_limited_length(sky, cam, 5) == pytest.approx(t5, abs=1.5)
    assert ea.sky_limited_length(sky, cam, 10) == pytest.approx(t10, abs=1.5)


def test_read_noise_penalty_and_sky_over_rn2():
    cam = _piggy()
    assert ea.rn_penalty_pct(120, 0.339, cam) == pytest.approx(6.5, abs=0.1)
    assert ea.rn_penalty_pct(300, 0.69, cam) == pytest.approx(1.4, abs=0.1)
    assert ea.rn_penalty_pct(400, 0.46, cam) == pytest.approx(1.5, abs=0.1)
    assert ea.sky_over_rn2(120, 0.339, cam) == pytest.approx(7.0, abs=0.1)
    assert ea.sky_over_rn2(400, 0.46, cam) == pytest.approx(31, abs=0.5)


# grooming table: 60' outer disk 0.19 e-/s per 2x2 pixel, 10-03 evening sky
# 1.71 per 2x2 pixel, overhead A 45.6 s / B 11 s; dark 09-21 sky 1.23; the
# 50' arm 0.35
@pytest.mark.parametrize("t,snr_h,h10,h20,h20b,h20dark,h20arm", [
    (60, 5.6, 3.2, 12.9, 8.7, 10.3, 4.1),
    (120, 6.6, 2.3, 9.3, 7.4, 7.3, 2.9),
    (180, 7.0, 2.0, 8.2, 6.9, 6.3, 2.6),
    (300, 7.4, 1.8, 7.3, 6.6, 5.6, 2.3),
    (400, 7.5, 1.8, 7.0, 6.5, 5.4, 2.2),
    (600, 7.7, 1.7, 6.7, 6.4, 5.1, 2.1),
])
def test_snr_per_hour_table(t, snr_h, h10, h20, h20b, h20dark, h20arm):
    cam = _piggy()
    a = ea.snr_per_hour(0.19, 1.71, t, cam, 45.6)
    assert a == pytest.approx(snr_h, abs=0.05)
    assert ea.hours_to_snr(10, a) == pytest.approx(h10, abs=0.05)
    assert ea.hours_to_snr(20, a) == pytest.approx(h20, abs=0.05)
    b = ea.snr_per_hour(0.19, 1.71, t, cam, 11.0)
    assert ea.hours_to_snr(20, b) == pytest.approx(h20b, abs=0.06)
    dark = ea.snr_per_hour(0.19, 1.23, t, cam, 45.6)
    assert ea.hours_to_snr(20, dark) == pytest.approx(h20dark, abs=0.05)
    arm = ea.snr_per_hour(0.35, 1.71, t, cam, 45.6)
    assert ea.hours_to_snr(20, arm) == pytest.approx(h20arm, abs=0.05)


def test_acceptance_halves_the_subs_per_hour():
    cam = _piggy()
    full = ea.snr_per_hour(0.19, 1.71, 300, cam, 45.6)
    half = ea.snr_per_hour(0.19, 1.71, 300, cam, 45.6, acceptance=0.5)
    assert ea.hours_to_snr(20, half) == pytest.approx(
        2 * ea.hours_to_snr(20, full))


def test_combine_weights():
    cam = _piggy()
    w = ea.combine_weights((120, 300, 400), 1.71, cam, signal=0.35)
    assert w[120] == 1.0
    assert 2.6 <= w[300] <= 2.7 and 3.5 <= w[400] <= 3.6
    wd = ea.combine_weights((120, 300, 400), 1.23, cam, signal=0.35)
    assert 2.6 <= wd[300] <= 2.7 and 3.5 <= wd[400] <= 3.65
    # seconds plus a small read-noise bonus: above the exposure ratio
    assert w[300] > 300 / 120 and w[400] > 400 / 120


def test_progress_of_the_library_m31_set():
    """41 kept x 120 s + 42 x 400 s + 2 x 300 s: about SNR 20 at 60' (96%
    of the light for SNR 20, 43% for SNR 30) and about SNR 35 at 50'."""
    cam = _piggy()
    subs = [(120, 1.71)] * 41 + [(400, 1.71)] * 42 + [(300, 1.71)] * 2
    p20 = ea.progress(subs, 0.19, 20, cam)
    assert p20["snr"] == pytest.approx(19.7, abs=0.1)
    assert p20["pct_of_light"] == pytest.approx(96, abs=1.5)
    assert ea.progress(subs, 0.19, 30, cam)["pct_of_light"] == \
        pytest.approx(43, abs=1)
    assert ea.progress(subs, 0.35, 20, cam)["snr"] == pytest.approx(35, abs=1)
    # a sub without its own sky takes the default; none at all skips it
    p = ea.progress([(300, None), (300, 1.71)], 0.19, 20, cam,
                    default_sky_sp=1.71)
    assert p["subs_used"] == 2
    assert ea.progress([(300, None)], 0.19, 20, cam)["subs_used"] == 0


# --- the recommendation ----------------------------------------------------------

def test_recommends_300_s_on_the_m31_numbers():
    cam = _piggy()
    rows = ea.length_table(cam, 0.46, 1.71, signal=0.19, overhead_s=45.6)
    rec = ea.recommend_length(rows, cam)
    assert rec["exp_s"] == 300 and rec["basis"] == "snr"
    assert rec["best_exp_s"] == 600
    text = ea.headline(rec)
    assert text.startswith("Recommended 300 s: sky-limited")
    assert "from the best SNR per hour (600 s)" in text
    r120 = next(r for r in rows if r["exp_s"] == 120)
    assert r120["sky_limited"] and r120["rn_penalty_pct"] == 4.9
    assert next(r for r in rows if r["exp_s"] == 60)["sky_limited"] is False


def test_recommendation_respects_limits_and_acceptance():
    cam = _piggy()
    rows = ea.length_table(cam, 0.46, 1.71, signal=0.19, overhead_s=45.6,
                           sat_stars={300: 8.0, 400: 7.0, 600: 9.0})
    rec = ea.recommend_length(rows, cam)
    assert rec["exp_s"] == 180            # 300 s and up clip too many stars
    capped = ea.recommend_length(
        ea.length_table(cam, 0.46, 1.71, signal=0.19, overhead_s=45.6),
        cam, max_length=180)
    assert capped["exp_s"] == 180 and "capped at 180 s" in capped["reasons"]
    # 120 s keeps 90% of its subs, everything longer half: 120 s wins
    acc = {120: 0.9, 180: 0.5, 300: 0.5, 400: 0.5, 600: 0.5}
    rows = ea.length_table(cam, 0.46, 1.71, signal=0.19, overhead_s=45.6,
                           acceptance=acc)
    rec = ea.recommend_length(rows, cam)
    assert rec["exp_s"] == 120
    assert "weighted by the measured acceptance per length" in rec["reasons"]
    r = next(x for x in rows if x["exp_s"] == 300)
    assert r["hours_at_acceptance"] == pytest.approx(2 * r["hours_to_goal"],
                                                     abs=0.11)


def test_unmeasured_lengths_take_the_pooled_acceptance():
    """Only 300 s has acceptance data (50%): scoring the other lengths as
    100% would push the pick off 300 s just for lack of data."""
    cam = _piggy()
    rows = ea.length_table(cam, 0.46, 1.71, signal=0.19, overhead_s=45.6,
                           acceptance={300: 0.5})
    assert ea.recommend_length(rows, cam)["exp_s"] == 400
    assert ea.recommend_length(rows, cam,
                               default_acceptance=0.5)["exp_s"] == 300


def test_without_a_signal_the_sky_limited_rule_decides():
    cam = _piggy()
    rows = ea.length_table(cam, 0.339, 1.23)
    assert all(r["snr_per_hour"] is None for r in rows)
    rec = ea.recommend_length(rows, cam)
    assert rec["basis"] == "sky-limited" and rec["exp_s"] == 180
    assert "no feature signal set" in rec["reasons"][-1]


# --- the frame sky ------------------------------------------------------------------

def test_sky_level_ignores_a_bright_target():
    rng = np.random.default_rng(1)
    a = rng.normal(300.0, 5.0, (512, 512)).astype(np.float32)
    yy, xx = np.mgrid[0:512, 0:512]
    a += (2000.0 * np.exp(-((xx - 256) ** 2 + (yy - 256) ** 2)
                          / (2 * 60.0 ** 2))).astype(np.float32)
    assert ea.sky_level(a) == pytest.approx(300.0, abs=2.0)
    assert float(np.median(a)) > 305           # a frame median reads high
    assert ea.sky_level(a[:20, :20]) == pytest.approx(float(np.median(
        a[:20, :20])))


def test_bayer_sites_follow_the_pattern_and_offsets():
    assert ea.bayer_sites(None)[(0, 0)] == "R"
    assert ea.bayer_sites({"BAYERPAT": "BGGR"})[(0, 0)] == "B"
    off = ea.bayer_sites({"BAYERPAT": "RGGB", "XBAYROFF": 1})
    assert off[(0, 0)] == "G" and off[(0, 1)] == "R"


def _mosaic(sky=(300.0, 330.0, 330.0, 280.0), size=512, seed=2):
    """RGGB mosaic with a different sky per CFA site, plus noise."""
    rng = np.random.default_rng(seed)
    d = rng.normal(0.0, 4.0, (size, size)).astype(np.float32)
    for (r, c), v in zip(((0, 0), (0, 1), (1, 0), (1, 1)), sky):
        d[r::2, c::2] += v
    return d


def test_frame_sky_per_channel():
    cam = _piggy()
    d = _mosaic()
    adu = ea.frame_sky_adu(d, osc=True, header={"BAYERPAT": "RGGB"})
    assert adu["R"] == pytest.approx(300, abs=1)
    assert adu["G"] == pytest.approx(330, abs=1)
    assert adu["B"] == pytest.approx(280, abs=1)
    f = ea.frame_sky(d, {"BAYERPAT": "RGGB", "EXPTIME": 120.0}, cam,
                     osc=True)
    g = (330 - 256.5) * 0.74 / 120 - 0.023
    assert f["sky_e_s"] == pytest.approx(g, abs=0.01)
    assert set(f["sky_e_s_ch"]) == {"R", "G", "B"}
    assert f["rn_penalty_pct"] == pytest.approx(
        ea.rn_penalty_pct(120, f["sky_e_s"], cam), abs=0.01)
    sp = ea.superpixel_sky(f["sky_e_s"], f["sky_e_s_ch"])
    ch = f["sky_e_s_ch"]
    assert sp == pytest.approx(ch["R"] + 2 * ch["G"] + ch["B"])
    assert ea.superpixel_sky(0.4) == pytest.approx(1.6)
    # no exposure time: the ADU only
    nf = ea.frame_sky(d, {"BAYERPAT": "RGGB"}, cam, osc=True)
    assert nf["sky_e_s"] is None and nf["sky_adu"]["G"] > 300
    # a 2x2-mean frame (the MemoryError fallback): one mixed channel
    from photonscript.shared.star_shape import bin2x2_mean
    bf = ea.frame_sky(bin2x2_mean(d), {"EXPTIME": 120.0}, cam, osc=True,
                      binned_input=True)
    assert set(bf["sky_adu"]) == {"mean"} and bf["sky_e_s_ch"] is None
    assert bf["sky_adu"]["mean"] == pytest.approx(310, abs=1)


def test_measure_frame_records_the_sky_fields():
    cfg = PhotonScriptConfig(_env_file=None)
    pv = rigs.rig_config(cfg, rigs.PIGGYBACK)
    hdr = {"BAYERPAT": "RGGB", "EXPTIME": 400.0, **LCG}
    m = sm.measure_frame(_mosaic(), pv, "piggyback", osc=True, header=hdr)
    for k in ("sky_e_s", "sky_e_s_ch", "rn_penalty_pct"):
        assert k in sm.PARITY_KEYS and m[k] is not None, k
    assert m["sky_e_s"] == pytest.approx((330 - 256.5) * 0.74 / 400 - 0.023,
                                         abs=0.01)
    # mono RC16 HCG: bias 256 stand-in, gain 0.25
    rng = np.random.default_rng(4)
    d = rng.normal(356.0, 6.0, (256, 256)).astype(np.float32)
    mm = sm.measure_frame(d, cfg, header={"EXPTIME": 300.0})
    assert mm["sky_e_s"] == pytest.approx(100 * 0.25 / 300, abs=0.005)
    assert mm["sky_e_s_ch"] is None
    assert sm.measure_frame(d, cfg)["sky_e_s"] is None   # no header


def test_live_and_backfill_record_the_same_sky(tmp_path):
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    cfg = _pcfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "s_Ha_300s_0001.fits",
               _field())
    live = _live(cfg, f)
    back = _fast_grade(f, cfg, [])
    for k in ("sky_adu", "sky_e_s", "sky_e_s_ch", "rn_penalty_pct"):
        assert live.get(k) == back.get(k), k
    assert back["sky_e_s"] is not None and back["rn_penalty_pct"] > 0


def test_piggyback_graders_record_channel_sky(tmp_path):
    _need_sep()
    from photonscript.scheduler.runs import _fast_grade
    from photonscript.telescope_agent.image_validator import validate_image
    cfg = _pcfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "o_300s_0002.fits",
               _field(q_ratio=1.0, fwhm=3.0, size=400, n=10, bayer=True,
                      hot=0), BAYERPAT="RGGB", **LCG)
    live = validate_image(str(f), rigs.rig_config(cfg, "piggyback"),
                          rig="piggyback")
    back = _fast_grade(f, cfg, [], rig="piggyback")
    assert live.sky_e_s == back["sky_e_s"]
    assert live.sky_e_s_ch == back["sky_e_s_ch"]
    assert set(back["sky_e_s_ch"]) == {"R", "G", "B"}


# --- qa-rescore backfill -------------------------------------------------------------

def test_record_sky_fields_from_the_stored_background():
    cam = ea.CameraModel.from_config(PhotonScriptConfig(_env_file=None),
                                     "rc16")
    f = ea.record_sky_fields({"exp_s": 300, "background": 356.0}, cam)
    assert f["sky_e_s"] == pytest.approx(100 * 0.25 / 300, abs=1e-4)
    assert f["sky_src"] == "background" and f["rn_penalty_pct"] > 0
    assert ea.record_sky_fields({"exp_s": 300, "background": 356.0,
                                 "sky_e_s": 0.1}, cam) == {}
    assert ea.record_sky_fields({"exp_s": None, "background": 356.0},
                                cam) == {}


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _write_subs(cfg, rows, date=NIGHT):
    with open(runs.runs_dir(cfg) / f"{date}_subs.jsonl", "w",
              encoding="utf-8") as fh:
        fh.write("".join(json.dumps(r) + "\n" for r in rows))


def test_rescore_backfills_sky_only_with_apply(tmp_path):
    cfg = _cfg(tmp_path)
    recs = [{"file": f"LIGHT/a{i}.fits", "time": f"{NIGHT}T04:0{i}:00",
             "target": "Heart Nebula", "filter": "OIII", "exp_s": 300.0,
             "rig": "rc16", "background": 356.0, "hfr": 2.0, "ecc": 0.3,
             "stars": 300, "passed_qa": True, "reviewed": True,
             "review_source": "manual"} for i in range(3)]
    _write_subs(cfg, recs)
    dry = runs.rescore_night(cfg, NIGHT)
    assert dry["counts"]["sky_backfilled"] == 3
    assert all("sky_e_s" not in r for r in runs._load_subs(cfg, NIGHT))
    runs.rescore_night(cfg, NIGHT, apply=True)
    after = runs._load_subs(cfg, NIGHT)
    assert all(r["sky_src"] == "background" for r in after)
    assert after[0]["sky_e_s"] == pytest.approx(100 * 0.25 / 300, abs=1e-4)
    assert "sky_backfilled" not in runs.rescore_night(cfg, NIGHT)["counts"]


# --- overhead and acceptance ---------------------------------------------------------

def test_parse_time_handles_nina_and_live_stamps():
    assert ea.parse_time("2026-10-04T02:12:36.1226536").microsecond == 122653
    assert ea.parse_time("2026-10-04T02:12:36Z").second == 36
    assert ea.parse_time("") is None and ea.parse_time("junk") is None


def test_overhead_from_date_obs_gaps():
    """10-03: 35 s typical (the per-sub Center), 2 of 24 gaps about 160 s
    (autofocus); a gap longer than the sub is a break or a missing sub."""
    from datetime import datetime, timedelta
    t = datetime(2026, 10, 4, 2, 0, 0)
    subs = []
    gaps = [35.0] * 22 + [160.0] * 2 + [2000.0, 500.0]
    for g in gaps:
        subs.append((NIGHT, t.isoformat(), 400.0))
        t += timedelta(seconds=400 + g)
    subs.append((NIGHT, t.isoformat(), 400.0))
    o = ea.overhead_from_subs(subs)
    assert o["n"] == 24 and o["median_s"] == 35.0
    assert o["mean_s"] == pytest.approx((22 * 35 + 320) / 24, abs=0.1)
    assert o["used_s"] == o["mean_s"] and o["source"] == "measured"
    # a length change is not a gap; nothing usable = the default
    assert ea.overhead_from_subs([(NIGHT, "2026-10-04T02:00:00", 120.0),
                                  (NIGHT, "2026-10-04T02:02:01", 300.0)]
                                 )["source"] == "default"


def test_acceptance_by_length():
    a = ea.acceptance_by_length([(120, True)] * 6 + [(120, False)] * 6
                                + [(400.0, True)] * 3)
    assert a[120] == {"n": 12, "accepted": 6, "share": 0.5, "used": True}
    assert a[400]["used"] is False


# --- the API ---------------------------------------------------------------------------

M31 = {"name": "Andromeda Galaxy", "catalog_id": "M 31", "ra_hours": 0.712,
       "dec_degrees": 41.27, "object_type": "galaxy"}


def _m31_project(**kw):
    from photonscript.scheduler.project_store import osc_plan
    from photonscript.shared.models import CelestialTarget, ImagingProject
    cfg = PhotonScriptConfig(_env_file=None)
    return ImagingProject(id="m31", target=CelestialTarget(**M31),
                          exposure_plans=[osc_plan(20, cfg, exp_s=300)],
                          driving_rig="piggyback", **kw)


def _osc_rec(i, t0, exp_s=400.0, ok=True, gap=35.0, sky=0.46):
    from datetime import datetime, timedelta
    t = datetime.fromisoformat(t0) + timedelta(seconds=i * (exp_s + gap))
    return {"file": f"LIGHT/m31_{exp_s:.0f}_{i:03d}.fits",
            "time": t.isoformat(), "target": "M 31", "filter": "OSC",
            "exp_s": exp_s, "rig": "piggyback", "hfr": 2.5, "ecc": 0.3,
            "stars": 400, "passed_qa": ok, "reviewed": ok,
            "readout": "Low Conversion Gain", "sat_stars_pct": 0.1,
            "sky_e_s": sky, "rn_penalty_pct": 1.5,
            "sky_e_s_ch": {"R": sky * 0.98, "G": sky, "B": sky * 0.65}}


def _seed_m31(cfg):
    recs = [_osc_rec(i, f"{NIGHT}T03:00:00", ok=i % 2 == 0)
            for i in range(24)]
    recs += [_osc_rec(i, f"{NIGHT}T07:00:00", exp_s=120.0, gap=0.4,
                      sky=0.339) for i in range(12)]
    _write_subs(cfg, recs)


def test_target_light_budget(tmp_path):
    from photonscript.scheduler.light_budget import target_light_budget
    cfg = _cfg(tmp_path)
    _seed_m31(cfg)
    proj = _m31_project(goal_snr=20.0, feature_signal_e_s=0.19,
                        feature_note="outer disk 60'")
    b = target_light_budget(cfg, "M31", [proj])
    assert b["project"] and b["goal_snr"] == 20.0
    assert b["feature"] == {"signal_e_s": 0.19, "rig": "piggyback",
                            "note": "outer disk 60'"}
    (g,) = b["groups"]
    assert (g["rig"], g["filter"], g["subs"]) == ("piggyback", "OSC", 36)
    assert g["camera"]["read_noise_adu"] == 3.27
    assert g["sky_e_s"]["p50"] == pytest.approx(0.46, abs=0.001)
    assert g["overhead"]["source"] == "measured"
    assert g["acceptance"][400]["share"] == 0.5
    rows = {r["exp_s"]: r for r in g["table"]}
    assert rows[400]["measured_subs"] == 24 and rows[300]["measured_subs"] == 0
    assert rows[400]["sat_stars_pct"] == 0.1
    assert g["recommendation"]["basis"] == "snr"
    assert g["headline"].startswith("Recommended ")
    p = g["progress"]
    assert p["subs"] == 24 and p["goal_snr"] == 20.0 and p["snr"] > 0
    assert g["sky_limited_s"]["5"] is not None
    # rig / filter narrowing and an unknown target
    assert target_light_budget(cfg, "M31", [proj], rig="rc16")["groups"] == []
    assert target_light_budget(cfg, "NGC 1", [])["groups"] == []


def test_without_a_signal_or_sky(tmp_path):
    from photonscript.scheduler.light_budget import target_light_budget
    cfg = _cfg(tmp_path)
    _seed_m31(cfg)
    b = target_light_budget(cfg, "M31", [_m31_project()])
    (g,) = b["groups"]
    assert b["goal_snr"] == 20.0 and g["progress"] is None
    assert g["recommendation"]["basis"] == "sky-limited"
    # records graded before PS-117 (no sky)
    recs = [dict(_osc_rec(i, f"{NIGHT}T03:00:00")) for i in range(3)]
    for r in recs:
        for k in ("sky_e_s", "sky_e_s_ch", "rn_penalty_pct"):
            r.pop(k)
    _write_subs(cfg, recs)
    (g,) = target_light_budget(cfg, "M31", [_m31_project()])["groups"]
    assert g["table"] == [] and "qa-rescore" in g["headline"]


def test_light_budget_endpoint(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    _seed_m31(cfg)
    proj = _m31_project(feature_signal_e_s=0.19)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", type("S", (), {
        "projects": {proj.id: proj}})())
    r = TestClient(app.app).get("/api/target/light-budget?name=M31")
    assert r.status_code == 200
    d = r.json()
    assert d["groups"][0]["progress"]["goal_snr"] == 20.0
    page = TestClient(app.app).get("/target?name=M31")
    assert page.status_code == 200 and "/api/target/light-budget" in page.text
    assert "Light budget" in page.text


# --- the M31 decision and the per-target fields -------------------------------------

LEGACY_M31 = {"m31": {
    "id": "m31", "priority": 50, "budget_hours": 6.0, "active": True,
    "driving_rig": "piggyback", "target": M31,
    "exposure_plans": [{"filter_type": "OSC", "exposure_seconds": 120,
                        "count": 180, "gain": 100, "offset": 256,
                        "acquired": 76, "acquired_s": 41 * 120 + 42 * 400
                        + 2 * 300, "rig": "piggyback"}]}}


def _store(tmp_path, done=None):
    from photonscript.scheduler.project_store import ProjectStore
    (tmp_path / "projects.json").write_text(json.dumps(LEGACY_M31))
    (tmp_path / "project_migrations.json").write_text(json.dumps(
        done if done is not None else {"ps30_m31_osc": True}))
    return ProjectStore(_cfg(tmp_path))


def test_m31_goes_to_300_s_and_20_h_once(tmp_path):
    store = _store(tmp_path)
    m31 = store.projects["m31"]
    (osc,) = m31.exposure_plans
    assert osc.exposure_seconds == 300 and osc.count == 240   # 20 h
    assert osc.long_seconds_done() == 41 * 120 + 42 * 400 + 2 * 300
    assert osc.acquired == (41 * 120 + 42 * 400 + 2 * 300) // 300
    assert m31.budget_hours == 20.0
    assert (m31.goal_snr, m31.feature_signal_e_s, m31.feature_rig) == (
        20.0, 0.19, "piggyback")
    assert "60'" in m31.feature_note
    done = json.loads((tmp_path / "project_migrations.json").read_text())
    assert done["ps117_m31_light_budget"] is True
    # a later hand edit is never undone
    store.update("m31", osc_hours=10)
    store.update("m31", light_budget={"feature_signal_e_s": 0.35})
    again = _store_reload(tmp_path)
    assert again.projects["m31"].feature_signal_e_s == 0.35


def _store_reload(tmp_path):
    from photonscript.scheduler.project_store import ProjectStore
    return ProjectStore(_cfg(tmp_path))


def test_m31_light_budget_runs_after_ps30_on_one_load(tmp_path):
    store = _store(tmp_path, done={})
    # the PS-30 migration ran on this load too (it only converts RC16 plans;
    # this M31 already has an OSC plan), then PS-117
    m31 = store.projects["m31"]
    assert m31.exposure_plans[0].exposure_seconds == 300


def test_update_sets_and_clears_the_light_budget_fields(tmp_path):
    store = _store(tmp_path)
    p = store.update("m31", light_budget={"goal_snr": 30, "feature_rig":
                                          "rc16", "feature_note": "arm"})
    assert (p.goal_snr, p.feature_rig, p.feature_note) == (30.0, "rc16",
                                                           "arm")
    p = store.update("m31", light_budget={"goal_snr": None,
                                          "feature_signal_e_s": "",
                                          "feature_rig": "bogus"})
    assert p.goal_snr is None and p.feature_signal_e_s is None
    assert p.feature_rig is None


def test_patch_endpoint_carries_the_fields(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app
    store = _store(tmp_path)
    monkeypatch.setattr(app, "get_store", lambda: store)
    r = TestClient(app.app).patch("/api/projects2/m31",
                                  json={"feature_signal_e_s": 0.21,
                                        "goal_snr": 25})
    assert r.status_code == 200
    assert store.projects["m31"].feature_signal_e_s == 0.21
    assert store.projects["m31"].goal_snr == 25.0


# --- exposure-report and --camera-cal on synthetic Library frames ------------------

def _fits(path: Path, data: np.ndarray, **hdr):
    from astropy.io import fits
    h = fits.Header()
    for k, v in hdr.items():
        h[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.clip(data, 0, 65535).astype(np.uint16),
                    header=h).writeto(path, overwrite=True)
    return path


def _osc_light(rng, sky_e_s, t, size=(512, 768), gain=0.74, bias=256.5,
               rn_adu=3.27):
    ch = {"R": 0.98, "G": 1.0, "B": 0.65}
    d = np.empty(size, dtype=np.float64)
    for (r, c), name in ea.bayer_sites(None).items():
        e = rng.poisson(sky_e_s * ch[name] * t, size=d[r::2, c::2].shape)
        d[r::2, c::2] = bias + e / gain
    return d + rng.normal(0.0, rn_adu, size)


def test_exposure_report_on_a_synthetic_library(tmp_path):
    from photonscript.scheduler import exposure_report as er
    rng = np.random.default_rng(7)
    lib = tmp_path / "Library"
    # dark 0 so the synthetic sky is exactly what the model recovers
    cfg = _cfg(tmp_path, piggyback_dark_e_s=0.0)
    for i, (t, sky) in enumerate([(400.0, 0.46)] * 4 + [(120.0, 0.34)] * 3):
        stamp = f"2026-10-04T0{3 + i // 4}:{(i % 4) * 7:02d}:{i:02d}"
        _fits(lib / "M 31" / "OSC" / f"m31_{int(t)}_{i}.fits",
              _osc_light(rng, sky, t), EXPTIME=t, BAYERPAT="RGGB",
              READOUTM="Low Conversion Gain", **{"DATE-OBS": stamp})
    rep = er.exposure_report(cfg, "M31", library=str(lib), signal=0.19)
    assert rep["files"] == 7 and rep["measured"] == 7
    by_t = {r["exp_s"]: r for r in rep["groups"]}
    assert by_t[400]["sky_e_s"] == pytest.approx(0.46, rel=0.05)
    assert by_t[120]["sky_e_s"] == pytest.approx(0.34, rel=0.05)
    assert by_t[400]["sky_over_rn2"]["B"] < by_t[400]["sky_over_rn2"]["G"]
    # the model noise matches the measured sky noise (gain and RN right)
    assert by_t[400]["noise_meas_over_pred"] == pytest.approx(1.0, abs=0.06)
    assert rep["recommendation"]["exp_s"] in (180, 300)
    text = er.format_report(rep)
    assert "Recommended" in text and "sky-limited" in text
    # only the OSC folder, every other light
    assert er.exposure_report(cfg, "M31", library=str(lib), flt="Ha",
                              signal=0.19)["files"] == 0


def test_camera_cal_recovers_read_noise_and_gain(tmp_path):
    from photonscript.scheduler import exposure_report as er
    rng = np.random.default_rng(11)
    lib = tmp_path / "Library"
    base = lib / "piggyback" / "Calibration"
    shape = (512, 512)
    hdr = dict(READOUTM="Low Conversion Gain", GAIN=100, **{"CCD-TEMP": 0.1})
    for i in range(8):
        _fits(base / "BIAS" / "2026-10-01" / f"b{i}.fits",
              256.5 + rng.normal(0, 3.27, shape), EXPTIME=0.001, **hdr)
    for i in range(6):
        level_e = 15000.0 * (1 + 0.02 * i)
        flat = 256.5 + rng.poisson(level_e, shape) / 0.74 \
            + rng.normal(0, 3.27, shape)
        _fits(base / "FLAT" / "2026-10-01" / f"f{i}.fits", flat,
              EXPTIME=2.0, **hdr)
    for i in range(4):
        _fits(base / "DARK" / "2026-10-01" / f"d{i}.fits",
              256.5 + 3.8 + rng.normal(0, 3.3, shape), EXPTIME=120.0, **hdr)
    rep = er.camera_cal(_cfg(tmp_path), library=str(lib), rigs=("piggyback",))
    (r,) = rep["rigs"]["piggyback"].values()
    assert r["read_noise_adu"] == pytest.approx(3.27, rel=0.05)
    assert r["gain_e_adu"] == pytest.approx(0.74, rel=0.05)
    assert r["dark_adu_s"] == pytest.approx(3.8 / 120, rel=0.1)
    assert "piggyback LCG gain 100" in er.format_cal(rep)


def test_cli_exposure_report_needs_a_target(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    res = CliRunner().invoke(cli.app, ["exposure-report"])
    assert res.exit_code == 2
