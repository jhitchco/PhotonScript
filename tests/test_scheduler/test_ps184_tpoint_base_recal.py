"""PS-184: tpoint-stats shows the base model and a recal run separately,
which one is active, and the polar advice from the refracted pole.

fixtures/tpoint/2026-10-10/ are the real TheSky64 (build 14139) files read
from the scope PC on 2026-10-10 with GET /api/thesky/tpoint/file (read
only): the base run refitted at 13:10Z (337 obs in the fit, RMS 19.94",
ME -79.57", MA -160.06", refraction A 47.546" B -0.0564") and the recal
run saved at 13:00Z (50 obs, IH / ID / CH fitted, RMS 62.30"). TheSky's
Polar Alignment Report for that model: ME -79.6", "Recommended ME -76.1"
(refracted pole)", lower 3" / no adjustment; tpoint-stats said "raise
1.33'" before this ticket.
"""
import os
import shutil
import time
from pathlib import Path

import pytest

from photonscript.scheduler import tpoint_files as tf
from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures" / "tpoint"
F1010 = FIX / "2026-10-10"
BASE = ("TPoint base run in.dat", "TPoint base run outmod.dat")
RECAL = ("TPoint recal run in.dat", "TPoint recal run outmod.dat")


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


def _tree(tmp_path, ages: dict, src=F1010) -> Path:
    root = tmp_path / "TheSky Professional Edition 64"
    tp = root / "TPoint"
    tp.mkdir(parents=True)
    now = time.time()
    for name, age in ages.items():
        f = tp / name
        shutil.copy(src / name, f)
        os.utime(f, (now - age, now - age))
    return root


def _stats(tmp_path, ages, **kw):
    root = _tree(tmp_path, ages)
    return tf.stats(_cfg(tmp_path, thesky_user_dir=str(root), **kw))


def test_real_files_parse():
    b = tf.parse_model((F1010 / BASE[1]).read_bytes().decode("latin-1"))
    assert b["observations"] == 337 and b["sky_rms_arcsec"] == 19.9378
    assert b["terms"]["ME"]["value"] == -79.5716 and not b["terms"]["ME"]["fixed"]
    assert b["refraction"] == {"a_arcsec": 47.546, "b_arcsec": -0.0564}
    assert b["caption"] == "Paramount MX"
    r = tf.parse_model((F1010 / RECAL[1]).read_bytes().decode("latin-1"))
    assert r["observations"] == 50 and r["sky_rms_arcsec"] == 62.3023
    assert r["caption"] == "Paramount MX recalibration"
    assert [k for k, v in r["terms"].items() if not v["fixed"]] == ["IH", "ID", "CH"]
    assert r["terms"]["ME"]["fixed"]
    d = tf.parse_data((F1010 / BASE[0]).read_bytes().decode("latin-1"))
    assert d["site"] == {"temp_c": 23.94, "pressure_mb": 870.0, "height_m": 1300.0}


def test_refraction_matches_thesky_recommended_me():
    """R = A tan z + B tan^3 z at the pole altitude: TheSky's -76.1"."""
    r = tf.model_refraction(31.906944, {"a_arcsec": 47.546, "b_arcsec": -0.0564})
    assert r["refraction_arcsec"] == pytest.approx(76.1, abs=0.05)
    assert tf.model_refraction(31.9, {"a_arcsec": 0}) is None
    # site fallback: the standard atmosphere when the file's pressure is
    # TheSky's default 1000 mB at 1300 m; the file's own when plausible
    s = tf.site_refraction(31.9, {"temp_c": 23.94, "pressure_mb": 1000.0,
                                  "height_m": 1300.0})
    assert s["pressure_mb"] == pytest.approx(866.5, abs=0.5)
    assert "standard atmosphere" in s["source"]
    s2 = tf.site_refraction(31.9, {"temp_c": 23.94, "pressure_mb": 870.0,
                                   "height_m": 1300.0})
    assert s2["pressure_mb"] == 870.0
    assert s2["refraction_arcsec"] == pytest.approx(79.8, abs=0.3)
    s3 = tf.site_refraction(31.9, None, 1300.0)
    assert s3["temp_c"] == 10.0 and "no site line" in s3["source"]


def test_base_newer_than_recal_the_10_10_files(tmp_path):
    """On the scope PC the base run was refitted at 13:10Z after the recal
    (13:00Z): the base model is active, the recal is shown as older."""
    st = _stats(tmp_path, {BASE[0]: 60, BASE[1]: 60, RECAL[0]: 660, RECAL[1]: 660})
    assert st["model_file"].endswith(BASE[1]) and st["model_kind"] == "base"
    assert st["points"] == 356 and st["model_points"] == 337
    assert st["sky_rms_arcsec"] == 19.9378
    rc = st["recal"]
    assert rc["model_file"].endswith(RECAL[1]) and not rc["after_base"]
    assert rc["points"] == 51 and rc["model_points"] == 50
    assert rc["fitted"] == ["IH", "ID", "CH"] and rc["sky_rms_arcsec"] == 62.3023
    assert st["active_model"] == "base"
    pol = st["polar"]
    assert pol["me_arcsec"] == -79.5716
    assert pol["me_recommended_arcsec"] == pytest.approx(-76.1, abs=0.05)
    # TheSky: "lower 3" / no adjustment"
    assert "within 3\" of the target (above it), no adjustment" in pol["advice"][0]
    assert "raise" not in pol["advice"][0]
    assert ("west of the pole, move it east: loosen the LEFT azimuth knob "
            "first, then tighten the RIGHT one") in pol["advice"][1]
    assert pol["total_arcmin"] == pytest.approx(2.67, abs=0.01) and pol["ok"]
    assert "confirm with TheSky's Polar Alignment Report" in pol["note"]
    text = tf.format_stats(st)
    assert text.startswith("TPoint base model (newest full run)")
    assert "recal run BEFORE it (belongs to an older base, not active)" in text
    assert "active model (ProTrack uses it): the base model" in text
    assert "from the refracted pole (R 76.1\"" in text


def test_recal_after_the_base_is_the_active_model(tmp_path):
    """The evidence shape: a recal saved after the base. Both are shown,
    with dates; the recal is active; the polar advice uses the base's
    fitted ME / MA (the recal holds them fixed)."""
    st = _stats(tmp_path, {BASE[0]: 3600, BASE[1]: 3600, RECAL[0]: 60, RECAL[1]: 60})
    assert st["model_file"].endswith(BASE[1])          # never the recal
    assert st["sky_rms_arcsec"] == 19.9378 and st["points"] == 356
    assert st["recal"]["after_base"] and st["recal"]["has_data"]
    assert st["active_model"] == "recal"
    assert st["active_model_file"].endswith(RECAL[1])
    assert st["polar"]["terms_source"] == "ME from the base model, MA from the base model"
    assert st["polar"]["me_arcsec"] == -79.5716
    text = tf.format_stats(st)
    assert ("recal run after it: " in text and "51 points, 3 fitted (IH, ID, CH), "
            "sky RMS 62.3\"" in text)
    assert "active model (ProTrack uses it): the recal run" in text


def test_recal_only_folder_reports_it_as_the_model(tmp_path):
    st = _stats(tmp_path, {RECAL[0]: 60, RECAL[1]: 60})
    assert st["model_kind"] == "recal" and st["recal"] is None
    assert st["active_model"] == "recal"
    assert tf.format_stats(st).startswith("TPoint recal run (no base model file found)")


def test_recommended_me_line_wins(tmp_path):
    root = _tree(tmp_path, {BASE[0]: 60, BASE[1]: 60})
    (root / "TPoint" / "Polar Alignment Report.txt").write_text(
        "Polar alignment\nRecommended ME -60.0\n", encoding="ascii")
    st = tf.stats(_cfg(tmp_path, thesky_user_dir=str(root)))
    pol = st["polar"]
    assert pol["me_recommended_arcsec"] == -60.0
    assert pol["target"].startswith("TheSky's Recommended ME")
    assert "above the target, lower it" in pol["advice"][0]


def test_polar_knob_forms_and_no_adjust_band():
    east = tf.polar({"MA": {"value": 90.0}})
    assert ("east of the pole, move it west: loosen the RIGHT azimuth knob "
            "first, then tighten the LEFT one") in east["advice"][0]
    near = tf.polar({"MA": {"value": -4.0}})
    assert "no adjustment" in near["advice"][0]
    low = tf.polar({"ME": {"value": 20.0}}, refraction={
        "refraction_arcsec": 76.1, "pole_alt_deg": 31.9, "source": "x"})
    assert low["me_recommended_arcsec"] == -76.1
    assert "below the target, raise it" in low["advice"][0]


def test_history_logs_a_new_recal(tmp_path):
    cfg = _cfg(tmp_path)
    st = {"ok": True, "model_date": "2026-10-10", "points": 356,
          "sky_rms_arcsec": 19.9, "recal": None}
    tf.save(cfg, st)
    tf.save(cfg, st)
    assert len(tf.history(cfg)) == 1
    tf.save(cfg, {**st, "recal": {"model_date": "2026-10-11", "sky_rms_arcsec": 40.0}})
    h = tf.history(cfg)
    assert len(h) == 2 and h[-1]["recal_date"] == "2026-10-11"


@pytest.mark.parametrize("rel", [
    "photonscript/scheduler/tpoint_files.py",
    "tests/test_scheduler/test_ps184_tpoint_base_recal.py"])
def test_ascii_no_em_dash(rel):
    text = (ROOT / rel).read_text(encoding="utf-8")
    assert not [ln for ln in text.splitlines() if any(ord(c) > 127 for c in ln)]
