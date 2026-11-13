"""PS-108: the 0 to 100 sub score (shared.qa_score) and the pixel counts
both graders record (shared.pixel_stats). Pure functions, no FITS I/O."""

from types import SimpleNamespace

import numpy as np
import pytest

from photonscript.shared import pixel_stats as ps
from photonscript.shared import qa_rules as q
from photonscript.shared import qa_score as qs
from photonscript.shared.config import PhotonScriptConfig


def _cfg(**kw):
    return PhotonScriptConfig(_env_file=None, quality_eccentricity_max=0.6,
                              **kw)


def _card(cfg, rig="rc16", **m):
    base = dict(hfr=5.0, fwhm_arcsec=2.4, ecc=0.4, stars=150,
                background=400.0, exp_s=300.0, ccd_temp=0.2, exposure="ok",
                sat_stars_pct=0.0, clipped_pct=0.0)
    base.update(m)
    return q.evaluate(q.record_metrics(**base),
                      q.context(cfg, rig, "T", "Ha" if rig == "rc16" else "OSC"))


# ------------------------------------------------------------- weights file

def test_weights_file_has_a_weight_and_a_why_for_every_check_on_both_rigs():
    w = qs.weights(SimpleNamespace(qa_score_weights_file=""))
    assert "error" not in w, w.get("error")
    assert w["version"] == "score1"
    for rig in ("rc16", "piggyback"):
        for cid in q.CHECKS:
            spec = w[rig][cid]
            assert isinstance(spec["weight"], (int, float)) and spec["weight"] >= 0
            assert spec["why"].strip() and spec["why"].isascii(), (rig, cid)
    for name, c in w["caps"].items():
        assert 0 <= c["cap"] < 80 and c["why"].isascii(), name
    # the built-in fallback mirrors the shipped file
    for rig in ("rc16", "piggyback"):
        assert qs.rig_weights(w, rig) == qs.rig_weights(qs._FALLBACK, rig)
    assert {k: v["cap"] for k, v in w["caps"].items()} == \
        {k: v["cap"] for k, v in qs._FALLBACK["caps"].items()}


def test_missing_weights_file_falls_back_and_says_so(tmp_path):
    w = qs.weights(SimpleNamespace(qa_score_weights_file=str(tmp_path / "x.toml")))
    assert "error" in w and qs.rig_weights(w, "rc16")["ecc"] == 25
    cfg = _cfg(qa_score_weights_file=str(tmp_path / "x.toml"))
    c = _card(cfg)
    assert c.score.value == 100 and c.score.as_dict()["weights_error"]


def test_weights_file_override_changes_the_score(tmp_path):
    p = tmp_path / "w.toml"
    p.write_text('version = "t1"\n[rc16.ecc]\nweight = 1\nwhy = "x"\n'
                 '[rc16.hfr]\nweight = 1\nwhy = "x"\n', encoding="utf-8")
    cfg = _cfg(qa_score_weights_file=str(p))
    c = _card(cfg, hfr=9.5)            # r 0.95 of 10 px: q = 0.67
    assert c.score.version == "t1"
    assert c.score.value == round(100 * (1 + (1.25 - 0.95) / 0.45) / 2)


# ------------------------------------------------------------------ grading

def test_ramp_full_inside_half_at_the_limit_zero_past_it():
    assert qs.ramp(0.5, 0.8, 1.25) == 1.0
    assert qs.ramp(1.0, 0.8, 1.25) == pytest.approx(0.556, abs=1e-3)
    assert qs.ramp(1.3, 0.8, 1.25) == 0.0


def test_all_green_rc16_scores_100_and_approves():
    c = _card(_cfg())
    assert c.score.value == 100 and c.score.decision == "approve"
    assert c.score.deductions == [] and c.score.cap is None
    assert c.verdict == "approved"


def test_slightly_over_a_gate_waits_for_review_far_over_rejects():
    cfg = _cfg()
    near = _card(cfg, ecc=0.62)
    assert near.drivers == ["ecc"]
    assert near.score.cap[:2] == ["out_of_gate", 79]
    assert near.score.value <= 79 and near.score.decision == "review"
    far = _card(cfg, ecc=0.80)
    assert far.score.cap[:2] == ["far_out_of_gate", 59]
    assert far.score.decision == "reject"


def test_inside_the_gate_still_costs_points_near_the_limit():
    cfg = _cfg()
    assert _card(cfg, ecc=0.45).score.value == 100     # r 0.75: full marks
    mid = _card(cfg, ecc=0.57)                          # r 0.95: warn band
    assert 80 <= mid.score.value < 100 and mid.score.deductions[0][0] == "ecc"


def _rows(**over):
    base = {cid: [cid, None, None, "skip"] for cid in q.CHECKS}
    base.update({"hfr": ["hfr", 4.0, 10.0, "pass"], "stars": ["stars", 200, [5, 5000], "pass"],
                 "roof": ["roof", "sky", "sky frame", "pass"]})
    for k, v in over.items():
        base[k] = [k] + list(v)
    return list(base.values())


@pytest.mark.parametrize("over, metrics, cap", [
    ({"roof": ("roof-closed", "sky frame", "fail")}, {}, ["roof", 0]),
    ({"pointing": (40.0, 15.0, "fail")},
     {"pointing_note": "N from M31 (solved RA 00h42m Dec +41.3)",
      "pointing_src": "solve"},
     ["pointing_solved", 20]),
    ({"slew_straddle": (35, 0, "fail")}, {}, ["slew_straddle", 20]),
    ({"temp": (12.0, 5.0, "fail")}, {}, ["temp", 40]),
    ({"tracking_jump": (0.4, 0.25, "fail")}, {}, ["tracking_jump", 40]),
    ({"stars": (2, [5, 5000], "fail")}, {}, ["stars", 40]),
    ({"guide_lock": ("non-star", "star", "fail")}, {}, ["guide_lock", 40]),
])
def test_hard_fail_caps(over, metrics, cap):
    t = q.thresholds(_cfg(), "piggyback")
    s = qs.score(_rows(**over), metrics, t, qs._FALLBACK)
    assert s.cap[:2] == cap
    assert s.value <= cap[1] and s.decision == "reject"


def test_mount_only_off_target_is_not_a_hard_cap():
    """PS-108 + PS-107: only a plate-solve-confirmed off target is a hard
    cap. A mount-log position (can be wrong by about 1 deg) just over the
    header-only reject (5 deg) only holds the sub for review; the source,
    not the note text, decides (a "solved" note without src stays
    unconfirmed)."""
    t = q.thresholds(_cfg(), "rc16")
    s = qs.score(_rows(pointing=(360.0, 300.0, "fail")),
                 {"pointing_note": "N from M31 (solved? mount RA 00h42m)",
                  "pointing_src": "mount-log"},
                 t, qs._FALLBACK)
    assert s.cap[:2] == ["out_of_gate", 79] and s.decision == "review"


def _point_card(cfg, off, src):
    return _card(cfg, pointing_offset_arcmin=off, pointing_src=src,
                 pointing_note="from M31")


def test_header_only_60_arcmin_warns_small_deduction_no_cap():
    """PS-107: a header offset of 60' is a WARN (unconfirmed), not a FAIL;
    the score grades it against the 5 deg header limit: a small deduction,
    never a cap."""
    c = _point_card(_cfg(), 60.0, "header")
    row = next(x for x in c.checks if x.id == "pointing")
    assert row.status == "warn" and "header only" in row.reason
    assert c.score.cap is None
    ded = {d[0]: d[1] for d in c.score.deductions}
    assert 0 < ded["pointing"] < 5
    assert c.score.value >= 95 and c.score.decision == "approve"


def test_header_only_gross_miss_67_deg_fails_and_rejects():
    """PS-107: 67 deg (the Dec 0 calibration spot) fails even from the
    header; not a solve, so the general far-out-of-gate cap rejects it."""
    c = _point_card(_cfg(), 67 * 60.0, "header")
    row = next(x for x in c.checks if x.id == "pointing")
    assert row.status == "fail"
    assert c.score.cap[:2] == ["far_out_of_gate", 59]
    assert c.score.decision == "reject"


def test_solve_20_arcmin_fails_with_the_hard_cap():
    c = _point_card(_cfg(), 20.0, "solve")
    row = next(x for x in c.checks if x.id == "pointing")
    assert row.status == "fail"
    assert c.score.cap[:2] == ["pointing_solved", 20]
    assert c.score.value <= 20 and c.score.decision == "reject"


def test_regrade_pointing_rescores_with_the_new_source():
    """The dawn pointing pass swaps the row with the solve's source and the
    score follows it: header warn (no cap) -> solve fail (hard cap)."""
    cfg = _cfg()
    t = q.thresholds(cfg, "rc16")
    c = _point_card(cfg, 20.0, "header")
    rec = {**q.record_metrics(hfr=5.0, fwhm_arcsec=2.4, ecc=0.4, stars=150,
                              background=400.0, exp_s=300.0, ccd_temp=0.2,
                              exposure="ok", pointing_offset_arcmin=20.0,
                              pointing_src="header"), "rig": "rc16"}
    rec.update(c.record_fields())
    assert rec["score"] >= 80
    f = q.regrade_pointing(rec, 20.0, t, "from M31 (solved)", "solve")
    assert f["pointing_src"] == "solve"
    assert f["score"] <= 20 and f["scorecard"]["score"]["cap"][0] ==         "pointing_solved"


def test_panel_pointing_row_names_the_source():
    cfg = _cfg()
    c = _point_card(cfg, 60.0, "header")
    rows = qs.panel_rows({"pointing_src": "header"}, q.expand(c.compact()),
                         c.thresholds, c.score.as_dict())
    pt = next(r for r in rows if r["id"] == "pointing")
    assert "(header, unconfirmed)" in pt["note"] and pt["status"] == "amber"
    c2 = _point_card(cfg, 3.0, "solve")
    rows = qs.panel_rows({"pointing_src": "solve"}, q.expand(c2.compact()),
                         c2.thresholds, c2.score.as_dict())
    assert "(plate solve)" in next(r for r in rows if r["id"] == "pointing")["note"]


def test_skipped_checks_take_no_weight():
    t = q.thresholds(_cfg(), "rc16")
    s = qs.score(_rows(), {}, t, qs._FALLBACK)
    assert s.value == 100


# --------------------------------------------------------- preview vs on

def test_preview_changes_no_verdict_but_records_the_score():
    cfg = _cfg()
    for c in (_card(cfg, ecc=0.62), _card(cfg, "piggyback", hfr=3.0)):
        f = c.record_fields()
        assert f["score"] == c.score.value
        assert f["score_decision"] == c.score.decision
        assert f["scorecard"]["score"]["s"] == c.score.value
    near = _card(cfg, ecc=0.62).record_fields()
    assert near["passed_qa"] is False and near["auto_verdict"] == "rejected"
    pig = _card(cfg, "piggyback", hfr=3.0).record_fields()
    assert pig["score_decision"] == "approve"
    assert "reviewed" not in pig          # Piggy-600 all-green stays manual today


def test_on_mode_score_sets_the_verdict_on_both_rigs():
    cfg = _cfg(qa_score_mode="on")
    near = _card(cfg, ecc=0.62)
    f = near.record_fields()
    assert f["passed_qa"] is True and f["reason"] == ""
    assert f["auto_verdict"] == "needs-look" and "reviewed" not in f
    assert f["drivers"] == ["ecc"] and "Eccentricity" in f["auto_reason"]
    pig = _card(cfg, "piggyback", hfr=3.0).record_fields()
    assert pig["reviewed"] is True and pig["review_source"] == "auto"
    far = _card(cfg, ecc=0.80).record_fields()
    assert far["passed_qa"] is False and "score 59 < 60" in far["reason"]


def test_on_mode_warnings_alone_can_reject():
    """Many small problems add up: no single gate fails, the score is under
    60 and the sub is rejected with the score as the reason."""
    cfg = _cfg(qa_score_mode="on", qa_score_reject=95.0, qa_score_approve=99.0)
    c = _card(cfg, ecc=0.57, hfr=9.6)
    assert not c.drivers and c.score.value < 95
    f = c.record_fields()
    assert f["passed_qa"] is False and f["reason"].startswith("score ")


def test_m31_osc_sub_from_2026_10_03_would_approve():
    """Jeremy's screenshot (M31 sub 6/23, Piggy-600 OSC 400 s, HFR 3.12, 400
    stars, bkg 547, sat-stars 8.5%, swamp 106 x RN^2): review today (the
    exposure warning); the score approves it with exposure as the top
    deduction."""
    cfg = _cfg()
    c = _card(cfg, "piggyback", hfr=3.12, stars=400, background=547.0,
              exp_s=400.0, exposure="sat-stars", sat_stars_pct=8.5, swamp=106,
              ecc=0.45, fwhm_arcsec=None, clipped_pct=0.01, ccd_temp=0.1)
    assert c.verdict == "needs-look"
    assert c.score.decision == "approve" and c.score.value == 90
    assert c.score.deductions[0][0] == "exposure"
    assert "sat stars 8.5% vs 5%" in c.score.deductions[0][2]


def test_swap_row_rescores():
    cfg = _cfg()
    t = q.thresholds(cfg, "piggyback")
    rec = {**q.record_metrics(hfr=3.0, stars=300, background=500.0, exp_s=300,
                              ecc=0.4, exposure="ok"), "rig": "piggyback"}
    rec.update(_card(cfg, "piggyback", hfr=3.0, stars=300).record_fields())
    assert rec["score"] == 100
    f = q.regrade_slew_straddle(rec, 40.0, t, "RC16 slew")
    assert f["score"] == 20 and f["score_decision"] == "reject"


# ------------------------------------------------------------- side panel

def test_panel_rows_carry_value_gate_and_status():
    cfg = _cfg()
    c = _card(cfg, ecc=0.62)
    rec = {"corner_spread": 0.5, "sat_px": 12, "sat_px_pct": 0.0004,
           "zero_px": 0, "zero_px_pct": 0.0, "max_adu": 65535.0,
           "sat_adu": 65000.0, "bg_median": 401.0, "bg_mad": 8.5,
           "sat_stars_pct": 1.0, "swamp": 9.0}
    rows = qs.panel_rows(rec, q.expand(c.compact()), c.thresholds,
                         c.score.as_dict())
    by = {r["id"]: r for r in rows}
    assert by["ecc"]["status"] == "red" and by["ecc"]["gate"] == "<= 0.6"
    assert by["ecc"]["lost"] > 0 and 0 < by["ecc"]["frac"] <= 1
    assert by["sat_px"]["status"] == "green" and "12 px" in by["sat_px"]["note"]
    assert by["max_adu"]["status"] == "amber"
    assert by["corner_spread"]["status"] == "amber"
    assert by["background"]["value"] == "401" and "MAD 8.5" in by["background"]["note"]
    assert by["stars"]["gate"] == "5 to 5000"
    legacy = {r["id"]: r for r in qs.panel_rows({}, q.expand(c.compact()),
                                                c.thresholds)}
    assert legacy["sat_px"]["status"] == "none"


# -------------------------------------------------------------- pixel stats

def _phys():
    a = np.full((40, 30), 600, dtype=np.uint16)
    a[0:2, 0:5] = 65535          # 10 saturated
    a[5, 5:8] = 65000            # 3 at the level
    a[10, 0:4] = 0               # 4 black
    return a


def test_frame_stats_raw_bzero_equals_physical():
    phys = _phys()
    raw = (phys.astype(np.int32) - 32768).astype(np.int16)
    a = ps.frame_stats(phys.astype(np.float32), 65000.0)
    b = ps.frame_stats(raw, 65000.0, bzero=32768.0, bscale=1.0)
    for k in ("sat_px", "zero_px", "max_adu", "bg_median", "bg_mad"):
        assert a[k] == b[k], k
    assert a["sat_px"] == 13 and a["zero_px"] == 4 and a["max_adu"] == 65535.0
    assert a["sat_px_pct"] == pytest.approx(100 * 13 / 1200, abs=1e-4)
    assert a["bg_median"] == 600.0


def test_saturation_level_header_wins():
    cfg = SimpleNamespace(qa_saturation_adu=65000.0)
    assert ps.saturation_level({"SATURATE": 60000}, cfg) == 60000.0
    assert ps.saturation_level({}, cfg) == 65000.0
    assert ps.saturation_level(None, cfg) == 65000.0


def test_histogram_exact_counts_and_clip():
    phys = _phys()
    raw = (phys.astype(np.int32) - 32768).astype(np.int16).astype(">i2")
    h = ps.histogram(raw, bzero=32768, bscale=1, sat_adu=65000, offset=256)
    assert sum(h["channels"]["L"]["counts"]) <= phys.size
    assert h["black_clip_pct"] == pytest.approx(100 * 4 / 1200, abs=1e-3)
    assert h["white_clip_pct"] == pytest.approx(100 * 13 / 1200, abs=1e-3)
    assert h["median"] == 600 and h["bias_floor"] == 262
    assert h["at_bias_floor_pct"] == pytest.approx(100 * 4 / 1200, abs=1e-2)


def test_histogram_bayer_channels():
    a = np.zeros((20, 20), dtype=np.uint16)
    a[0::2, 0::2] = 1000     # R
    a[0::2, 1::2] = 2000     # G
    a[1::2, 0::2] = 2000     # G
    a[1::2, 1::2] = 3000     # B
    h = ps.histogram(a.astype(np.float32), bayer="RGGB")
    meds = {k: v["median"] for k, v in h["channels"].items()}
    assert meds == {"R": 1000, "G": 2000, "B": 3000}
    assert h["channels"]["G"]["n"] == 200
