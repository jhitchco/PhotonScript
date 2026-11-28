"""PS-190: the ZWO ASI174MM Mini replaces the OGMA GP678C on the RC16 OAG.

The desired PHD2 file knows both cameras ([[camera]]); the audit picks the
connected one by name and judges its rows (binning 1, 5.86 um, 2 s start,
gain band, HFD floor, search region, the expected 0.372"/px scale), the
GP678C keeps today's rows until the swap, and the guide pixel scale from
config follows the camera named in the PHD2 header.
"""
from pathlib import Path

import pytest

from photonscript.scheduler import phd2_audit as pa
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler import tracking_drift as td
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent.phd2_client import guide_scale_from_config

FIX = Path(__file__).parent / "fixtures" / "phd2"
N26 = "PHD2_GuideLog_2026-09-26_120453.txt"


def _cfg(**kw):
    return PhotonScriptConfig(_env_file=None, **kw)


def _eval(observed, cfg=None):
    cfg = cfg or _cfg()
    obs = {s: {} for s in pa.SOURCES}
    obs.update(observed)
    res = pa.evaluate(pa.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}, res


ASI = {"camera": "ZWO ASI174MM Mini", "binning": 1, "pixel_scale": 0.37,
       "exposure_ms": 2000, "search_region_px": 25}


def test_desired_file_knows_both_cameras_and_lints_clean():
    d = pa.load_desired(_cfg())
    assert not d.get("lint"), d.get("lint")
    cams = {c["id"]: c for c in pa.cameras(d)}
    assert set(cams) == {"gp678c", "asi174mm_mini"}
    asi = cams["asi174mm_mini"]
    assert asi["pixel_size_um"] == 5.86 and asi["binning"] == 1
    assert asi["expected_scale_arcsec_px"] == pytest.approx(206.265 * 5.86 / 3248, abs=0.001)
    assert pa.default_camera(d)["id"] == "gp678c"
    assert all(b < 128 for b in pa.DEFAULT_FILE.read_bytes())


def test_lint_catches_bad_camera_tables():
    d = {"check": [{"id": "x", "group": "G", "label": "L", "sources": ["api"],
                    "equals": 1, "severity": "warn", "apply": "manual",
                    "why": "w", "fix": "f"}],
         "default_camera": "nope",
         "camera": [{"id": "a", "match": "", "rows": {"y": {"equals": 2}}},
                    {"id": "a", "match": "A", "pixel_size_um": 2.0}]}
    p = " | ".join(pa.lint_desired(d))
    for frag in ("no match", "no pixel_size_um", "duplicate camera id",
                 "unknown row y", "default_camera"):
        assert frag in p, frag


@pytest.mark.parametrize("name,want,detected", [
    ("ZWO ASI174MM Mini", "asi174mm_mini", True),
    ("ZWO ASI174MM-Mini(CMOS)", "asi174mm_mini", True),
    ("OGMA GP678C", "gp678c", True),
    ("GP678C", "gp678c", True),
    ("OGMA AP26CC", "gp678c", False),        # unknown: default rows, row fails
    (None, "gp678c", False),
])
def test_select_camera(name, want, detected):
    d = pa.load_desired(_cfg())
    cam, rep, det = pa.select_camera(d, {"api": {"camera": name} if name else {}})
    assert cam["id"] == want and det is detected and rep == name


def test_camera_row_accepts_either_camera_until_the_swap():
    for name in ("OGMA GP678C", "ZWO ASI174MM Mini"):
        rows, res = _eval({"api": {"camera": name}})
        assert rows["camera"]["status"] == pa.PASS, (name, rows["camera"])
        assert "ASI174MM Mini" in rows["camera"]["desired"]
        assert res["camera"]["detected"]
    rows, res = _eval({"api": {"camera": "OGMA AP26CC"}})
    assert rows["camera"]["status"] == pa.FAIL and not res["camera"]["detected"]
    rows, _ = _eval({})
    assert rows["camera"]["status"] == pa.UNKNOWN


def test_asi174_rows_are_applied_when_it_is_connected():
    rows, res = _eval({"api": ASI})
    assert res["camera"]["id"] == "asi174mm_mini"
    assert rows["binning"]["status"] == pa.PASS
    assert rows["exposure_ms"]["status"] == pa.PASS and rows["exposure_ms"]["target"] == 2000
    assert rows["search_region_px"]["status"] == pa.PASS
    assert rows["search_region_px"]["target"] == 25
    assert rows["guide_scale"]["status"] == pa.PASS
    assert "0.36" in rows["guide_scale"]["desired"] or "0.37" in rows["guide_scale"]["desired"]
    # the GP678C's settings on the new camera all miss
    old = dict(ASI, binning=2, pixel_scale=0.125, search_region_px=15, exposure_ms=1000)
    rows, _ = _eval({"api": old, "profile": {"pixel_size_um": 2.0, "min_hfd_px": 1.5}})
    assert rows["binning"]["status"] == pa.WARN and rows["binning"]["target"] == 1
    assert rows["pixel_size_um"]["status"] == pa.FAIL
    assert rows["pixel_size_um"]["target"] == 5.86
    assert rows["guide_scale"]["status"] == pa.WARN
    assert rows["search_region_px"]["status"] != pa.PASS
    assert rows["exposure_ms"]["status"] == pa.WARN
    rows, _ = _eval({"api": ASI, "profile": {"min_hfd_px": 2.0, "pixel_size_um": 5.86}})
    assert rows["min_hfd_px"]["status"] == pa.PASS and rows["pixel_size_um"]["status"] == pa.PASS


def test_gp678c_rows_are_unchanged():
    gp = {"camera": "OGMA GP678C", "binning": 2, "pixel_scale": 0.25,
          "exposure_ms": 3000, "search_region_px": 35}
    rows, res = _eval({"api": gp, "profile": {"pixel_size_um": 2.0}})
    assert res["camera"]["id"] == "gp678c"
    assert rows["binning"]["status"] == pa.PASS and rows["binning"]["target"] == 2
    assert rows["pixel_size_um"]["status"] == pa.PASS
    assert rows["exposure_ms"]["target"] == 3000
    assert rows["guide_scale"]["status"] == pa.PASS
    # same rows as the plain file when nothing reports a camera
    d = pa.load_desired(_cfg())
    assert pa.rows_for_camera(d, pa.default_camera(d)) == d["check"]


def test_gain_band_until_the_tuner_has_data():
    rows, _ = _eval({"api": dict(ASI, gain=40), "tuning": {"ok": False}})
    g = rows["gain"]
    assert g["status"] == pa.PASS and "33 to 50" in g["desired"]
    rows, _ = _eval({"api": dict(ASI, gain=90), "tuning": {"ok": False}})
    assert rows["gain"]["status"] == pa.WARN and rows["gain"]["target"] in (41, 42)
    rows, _ = _eval({"api": dict(ASI, gain=70),
                     "tuning": {"ok": True, "gain": 70, "note": "faint"}})
    assert rows["gain"]["status"] == pa.PASS and "outside" in rows["gain"]["note"]
    # the GP678C row stays info-only without tuner data
    rows, _ = _eval({"api": {"camera": "GP678C", "gain": 90}, "tuning": {"ok": False}})
    assert rows["gain"]["status"] == pa.INFO


def test_arcsec_rows_convert_at_the_new_scale():
    cfg = _cfg(guide_focal_length_mm=3248)
    rows, res = _eval({"api": ASI, "nina": {"nina_settle_px": 2.0}}, cfg)
    assert res["scale_arcsec_px"] == pytest.approx(0.3721, abs=1e-3)
    # 0.5 to 1.0 arcsec at 0.372 = 1.3 to 2.7 px: 2 px passes
    assert rows["nina_settle_px"]["status"] == pa.PASS
    rows, _ = _eval({"api": dict(ASI, camera="OGMA GP678C", binning=2),
                     "nina": {"nina_settle_px": 2.0}}, cfg)
    assert rows["nina_settle_px"]["status"] == pa.PASS     # 2 to 3.9 px at 0.254


def test_apply_uses_the_judged_cameras_rows(monkeypatch):
    d = pa.load_desired(_cfg())
    cam = next(c for c in pa.cameras(d) if c["id"] == "asi174mm_mini")
    rows = {r["id"]: r for r in pa.rows_for_camera(d, cam)}
    assert (rows["exposure_ms"]["min"], rows["exposure_ms"]["max"]) == (1500, 4000)
    assert "equals" not in rows["exposure_ms"]
    assert rows["binning"]["equals"] == 1 and "5.86" in rows["pixel_size_um"]["fix"]


def test_guide_scale_from_config_follows_the_camera_name():
    cfg = _cfg(guide_focal_length_mm=3248)
    assert guide_scale_from_config(cfg, 1) == pytest.approx(0.127, abs=1e-3)   # 2.0 um
    assert guide_scale_from_config(cfg, 1, camera="ZWO ASI174MM Mini") == \
        pytest.approx(0.372, abs=1e-3)
    assert guide_scale_from_config(cfg, 2, camera="OGMA GP678C") == \
        pytest.approx(0.254, abs=1e-3)
    assert guide_scale_from_config(cfg, 1, camera="Unknown Cam") == \
        pytest.approx(0.127, abs=1e-3)
    assert guide_scale_from_config(cfg, 1, pixel_um=5.86) == pytest.approx(0.372, abs=1e-3)
    assert guide_scale_from_config(_cfg(phd2_pixel_scale_arcsec=0.5), 1,
                                   camera="ZWO ASI174MM Mini") == 0.5


def test_guide_log_scale_for_an_asi174_header():
    """guide_motion / drift take the scale from the PHD2 header (pixel size
    x binning / focal length); without the header's pixel size the config
    fallback now follows the header's camera name."""
    cfg = _cfg(guide_focal_length_mm=3248)
    h = {"camera": "ZWO ASI174MM Mini", "binning": 1, "pixel_scale": None,
         "pixel_size_um": 5.86, "focal_length_mm": 3248}
    s, src = td.guide_scale(h, cfg)
    assert s == pytest.approx(0.372, abs=1e-3) and src.startswith("log")
    h2 = {"camera": "ZWO ASI174MM Mini", "binning": 1}
    s2, src2 = pl._scale_for(h2, cfg)
    assert s2 == pytest.approx(0.372, abs=1e-3) and src2 == "config"
    s3, _ = pl._scale_for({"camera": "OGMA GP678C", "binning": 2}, cfg)
    assert s3 == pytest.approx(0.254, abs=1e-3)


def test_0926_fixture_still_judged_as_the_gp678c():
    secs = pl.parse_guide_log((FIX / N26).read_text(encoding="utf-8"), N26)
    log = pa.log_observed(secs, _cfg())
    rows, res = _eval({"log": log})
    assert res["camera"]["id"] == "gp678c" and res["camera"]["detected"]
    assert rows["camera"]["status"] == pa.PASS
    assert rows["search_region_px"]["status"] == pa.FAIL      # 15 px, as before


def test_hardware_has_the_swap_checklist():
    text = (Path(__file__).resolve().parents[2] / "docs" / "HARDWARE.md").read_text(
        encoding="utf-8")
    i = text.index("ASI174MM Mini swap checklist")
    block = text[i:i + 4000]
    for word in ("OAG focus", "Dark library", "defect", "calibration",
                 "Guiding Assistant", "settle", "guide_camera_pixel_um"):
        assert word.lower() in block.lower(), word
