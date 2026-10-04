"""PS-21: the unified QA rules (shared.qa_rules) and the scorecard.

One entry per check, thresholds per rig with the PS-48 per-target hook,
auto-approve of all-green subs, and the backward-compatible reason string.
"""

import json

import pytest

from photonscript.shared import qa_rules as q
from photonscript.shared.config import PhotonScriptConfig


def _cfg(**kw):
    base = dict(_env_file=None, quality_eccentricity_max=0.6,
                quality_tracking_rms_max=2.0, piggyback_enabled=True)
    base.update(kw)
    return PhotonScriptConfig(**base)


GOOD = dict(hfr=5.0, fwhm_arcsec=2.4, ecc=0.40, stars=150, background=400.0,
            exp_s=300.0, ccd_temp=0.2, exposure="ok")


def _card(metrics=None, cfg=None, rig="rc16", **ctx):
    cfg = cfg or _cfg()
    m = dict(GOOD)
    m.update(metrics or {})
    return q.evaluate(m, q.context(cfg, rig, ctx.pop("target", None),
                                   ctx.pop("filter", None), **ctx))


def _row(card, cid):
    return next(c for c in card.checks if c.id == cid)


# ------------------------------------------------------------ thresholds

def test_thresholds_use_the_configured_ecc_not_the_code_default():
    assert PhotonScriptConfig(_env_file=None).quality_eccentricity_max == 0.70
    t = q.thresholds(_cfg(), "rc16")
    assert t["ecc_max"] == 0.6          # scope PC: PS_QUALITY_ECCENTRICITY_MAX
    assert _row(_card({"ecc": 0.64}), "ecc").limit == 0.6


def test_thresholds_are_per_rig():
    cfg = _cfg(camera_setpoint_c=-10.0, piggyback_setpoint_c=0.0)
    rc, pb = q.thresholds(cfg, "rc16"), q.thresholds(cfg, "piggyback")
    assert rc["hfr_max"] == 10.0 and pb["hfr_max"] == 4.5
    assert rc["fwhm_max"] == 4.0 and pb["fwhm_max"] == 6.0
    assert rc["fwhm_soft"] is False and pb["fwhm_soft"] is True
    assert rc["ecc_max"] == 0.6 and pb["ecc_max"] == 0.75
    assert rc["setpoint_c"] == -10.0 and pb["setpoint_c"] == 0.0
    assert pb["pixel_scale"] == 1.29


def test_rig_view_config_gives_the_same_thresholds():
    """The telescope agent for the piggyback holds rig_config(...) already;
    resolving again must not change a limit."""
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    cfg = _cfg()
    a = q.thresholds(cfg, PIGGYBACK)
    b = q.thresholds(rig_config(cfg, PIGGYBACK), PIGGYBACK)
    assert a == b


def test_per_target_override_hook_ps48():
    ov = {"Cat's Eye Nebula": {"hfr_max": 6.0},
          "cat's eye nebula|OIII": {"ecc_max": 0.5},
          "piggyback:M31": {"hfr_max": 3.5}}
    cfg = _cfg(qa_target_overrides=json.dumps(ov))
    t = q.thresholds(cfg, "rc16", "Cat's Eye Nebula", "Ha")
    assert t["hfr_max"] == 6.0 and t["ecc_max"] == 0.6
    assert t["override"] == ["hfr_max"]
    t = q.thresholds(cfg, "rc16", "Cat's Eye Nebula", "OIII")
    assert t["hfr_max"] == 6.0 and t["ecc_max"] == 0.5
    assert q.thresholds(cfg, "rc16", "M31")["hfr_max"] == 10.0
    assert q.thresholds(cfg, "piggyback", "M31")["hfr_max"] == 3.5
    card = _card({"hfr": 7.0}, cfg=cfg, target="Cat's Eye Nebula", filter="Ha")
    assert _row(card, "hfr").status == q.FAIL
    assert q.thresholds(_cfg(qa_target_overrides="not json"), "rc16", "x")[
        "hfr_max"] == 10.0


# ---------------------------------------------------------------- checks

def test_all_green_is_approved_and_auto_approved():
    card = _card()
    assert card.verdict == q.APPROVED and card.passed and card.reason == ""
    f = card.record_fields()
    assert f["passed_qa"] is True and f["reviewed"] is True
    assert f["review_source"] == "auto" and f["auto_verdict"] == "approved"
    assert f["scorecard"]["verdict"] == "approved"
    ids = [r[0] for r in f["scorecard"]["rows"]]
    assert ids == list(q.CHECKS)          # the big list, in order, every sub


def test_auto_approve_can_be_turned_off():
    f = _card(cfg=_cfg(qa_auto_approve=False)).record_fields()
    assert f["auto_verdict"] == "approved" and "reviewed" not in f


def test_warn_stays_yellow_and_does_not_reject():
    card = _card({"ecc": 0.57})           # within 10% of 0.6
    assert _row(card, "ecc").status == q.WARN
    assert card.verdict == q.NEEDS_LOOK and card.passed
    f = card.record_fields()
    assert f["passed_qa"] is True and "reviewed" not in f
    assert f["reason"] == "" and f["drivers"] == []


def test_every_failing_check_is_recorded():
    card = _card({"ecc": 0.66, "hfr": 11.2, "fwhm_arcsec": 4.4,
                  "ccd_temp": 23.0, "set_temp": 20.0})
    assert card.verdict == q.REJECTED
    assert card.drivers == ["ecc", "hfr", "fwhm", "temp"]
    assert "Eccentricity 0.66 > 0.6 (trailing/drift)" in card.reason
    assert "HFR 11.2px > 10px (out of focus)" in card.reason
    assert "FWHM 4.4\" > 4\"" in card.reason
    assert "cooler failure; camera was set to 20C" in card.reason
    f = card.record_fields()
    assert f["auto_reason"] == f["reason"] == card.reason
    assert f["drivers"] == card.drivers and "reviewed" not in f


@pytest.mark.parametrize("ecc,status", [(0.30, "pass"), (0.55, "warn"),
                                        (0.60, "warn"), (0.61, "fail"),
                                        (None, "skip")])
def test_ecc(ecc, status):
    assert _row(_card({"ecc": ecc}), "ecc").status == status


@pytest.mark.parametrize("hfr,status", [(5.0, "pass"), (9.5, "warn"),
                                        (10.5, "fail"), (None, "skip")])
def test_hfr_abs(hfr, status):
    assert _row(_card({"hfr": hfr}), "hfr").status == status


def test_hfr_vs_night_median():
    night = {"hfr_median": 5.0, "n_hfr": 12, "bg_median": 400.0, "n_bg": 12}
    assert _row(_card({"hfr": 6.9}, night=night), "hfr_rel").status == q.PASS
    c = _card({"hfr": 7.2}, night=night)
    r = _row(c, "hfr_rel")
    assert r.status == q.FAIL and r.limit == 7.0
    assert "HFR outlier: 7.2 vs night median 5 (x1.4 limit)" in c.reason
    few = {"hfr_median": 5.0, "n_hfr": 3}
    assert _row(_card({"hfr": 9.0}, night=few), "hfr_rel").status == q.SKIP
    assert _row(_card({"hfr": 9.0}), "hfr_rel").status == q.SKIP


def test_fwhm_hard_on_rc16_advisory_on_piggyback():
    assert _row(_card({"fwhm_arcsec": 4.3}), "fwhm").status == q.FAIL
    pb = _card({"fwhm_arcsec": 7.8, "hfr": 2.7, "ecc": 0.4}, rig="piggyback")
    assert _row(pb, "fwhm").status == q.WARN and pb.passed
    assert _row(_card({"fwhm_arcsec": None}), "fwhm").status == q.SKIP


@pytest.mark.parametrize("n,status", [(4, "fail"), (5, "pass"), (5000, "pass"),
                                      (5001, "fail")])
def test_star_count(n, status):
    c = _card({"stars": n})
    assert _row(c, "stars").status == status
    if n == 4:
        assert "Only 4 stars detected (minimum 5)" in c.reason
    assert _row(c, "stars").limit == [5, 5000]


def test_background_vs_night_median_warns_only():
    night = {"hfr_median": 5.0, "n_hfr": 10, "bg_median": 400.0, "n_bg": 10}
    c = _card({"background": 900.0}, night=night)
    assert _row(c, "bg_rel").status == q.WARN and c.passed
    assert c.verdict == q.NEEDS_LOOK
    assert _row(_card({"background": 700.0}, night=night),
                "bg_rel").status == q.PASS


def test_background_vs_bias_floor():
    short = _card({"background": 257.0, "exp_s": 60.0})
    assert _row(short, "bg_floor").status == q.WARN
    assert short.passed          # 60 s narrowband at the floor is legitimate
    assert _row(_card(), "bg_floor").limit == 262.0     # offset 256 + 6


def test_sensor_temp_vs_configured_setpoint():
    assert _row(_card({"ccd_temp": 0.9}), "temp").status == q.PASS
    assert _row(_card({"ccd_temp": 1.5}), "temp").status == q.WARN
    c = _card({"ccd_temp": 5.5})
    assert _row(c, "temp").status == q.FAIL and "cooler failure" in c.reason
    warm = _cfg(camera_setpoint_c=8.0)
    c = _card({"ccd_temp": 10.5}, cfg=warm)     # within +5 but over ceiling
    assert "above 10C limit" in c.reason
    assert _row(_card({"ccd_temp": None}), "temp").status == q.SKIP


def test_guide_rms_is_recorded_not_judged_until_ps70():
    c = _card({"guide_rms": 98.0, "guide_state": "guiding"})
    r = _row(c, "guide_rms")
    assert r.status == q.SKIP and r.value == 98.0 and "PS-70" in r.reason
    assert c.passed


def test_guide_rms_gate_once_enabled():
    cfg = _cfg(qa_guide_rms_mode="fail")
    c = _card({"guide_rms": 2.3, "guide_state": "guiding"}, cfg=cfg)
    assert _row(c, "guide_rms").status == q.FAIL
    assert "Tracking RMS 2.30\" > 2\"" in c.reason
    c = _card({"guide_rms": 2.3, "guide_state": "stopped"}, cfg=cfg)
    assert _row(c, "guide_rms").status == q.SKIP     # unguided: junk RMS
    c = _card({"guide_rms": 2.3, "guide_state": "guiding"},
              cfg=_cfg(qa_guide_rms_mode="warn"))
    assert _row(c, "guide_rms").status == q.WARN and c.passed


def test_tracking_jump():
    c = _card({"doubled_frac": 0.3})
    assert _row(c, "tracking_jump").status == q.FAIL
    assert "tracking jump: 30% of stars doubled" in c.reason
    assert _row(_card({"doubled_frac": 0.1}), "tracking_jump").status == q.PASS
    assert _row(_card(), "tracking_jump").status == q.SKIP


def test_exposure_clipping_warns_read_noise_is_a_note():
    assert _row(_card({"exposure": "clipped", "clipped_pct": 0.2}),
                "exposure").status == q.WARN
    r = _row(_card({"exposure": "under", "swamp": 1.8}), "exposure")
    assert r.status == q.PASS and "read-noise" in r.reason


def test_roof_closed_signature_reuses_ps71():
    """2026-09-26 sub 0024: OIII 300 s, bias-floor background, 12 'stars'
    of HFR 1.42 px / 0.57": a dark frame."""
    c = _card({"hfr": 1.42, "fwhm_arcsec": 0.57, "stars": 12,
               "background": 257.0, "exp_s": 300.0, "ecc": 0.5})
    r = _row(c, "roof")
    assert r.status == q.FAIL and c.qa_flag == "roof-closed"
    assert "dark-frame signature" in c.reason
    assert c.record_fields()["qa_flag"] == "roof-closed"


def test_roof_unsafe_window():
    from datetime import datetime
    wins = [(datetime(2026, 9, 27, 11, 39), datetime(2026, 9, 27, 12, 18))]
    c = _card(unsafe_windows=wins, start_utc=datetime(2026, 9, 27, 11, 50))
    assert _row(c, "roof").status == q.FAIL and "UNSAFE" in c.reason


def test_pointing_check_ps67():
    r = _row(_card(), "pointing")
    assert r.status == q.SKIP     # no position / target: skipped
    assert q.expand(_card().compact())[-1]["reason"].startswith("no position")
    assert _row(_card({"pointing_offset_arcmin": 9.0}),
                "pointing").status == q.WARN      # 8 to 15': a look
    assert _row(_card({"pointing_offset_arcmin": 16.0,
                       "pointing_src": "solve"}),
                "pointing").status == q.FAIL      # target out of the frame
    assert _row(_card({"pointing_offset_arcmin": 16.0}),
                "pointing").status == q.WARN      # PS-107: unconfirmed


def test_nan_and_garbage_inputs_skip_instead_of_crashing():
    c = _card({"hfr": float("nan"), "ecc": "x", "background": None})
    assert _row(c, "hfr").status == q.SKIP and _row(c, "ecc").status == q.SKIP


# ------------------------------------------------------- storage helpers

def test_compact_is_small_and_expands_to_the_big_list():
    card = _card({"ecc": 0.66})
    comp = card.compact()
    assert len(json.dumps(comp)) < 900
    rows = q.expand(comp)
    assert [r["id"] for r in rows] == list(q.CHECKS)
    e = next(r for r in rows if r["id"] == "ecc")
    assert e == {"id": "ecc", "name": "Eccentricity", "value": 0.66,
                 "unit": "", "limit": 0.6, "status": "fail",
                 "reason": "Eccentricity 0.66 > 0.6 (trailing/drift)",
                 "why": q.CHECKS["ecc"][2]}


def test_metrics_from_record_drops_the_backfill_pseudo_fwhm():
    live = {"hfr": 5, "fwhm_arcsec": 2.4}
    back = {"hfr": 5, "fwhm_arcsec": 1.2, "graded_by": "sep-binned"}
    assert q.metrics_from_record(live)["fwhm_arcsec"] == 2.4
    assert q.metrics_from_record(back)["fwhm_arcsec"] is None


def test_night_context_groups_by_rig_target_filter():
    recs = [{"rig": "rc16", "target": "A", "filter": "Ha", "hfr": h,
             "background": 300 + h} for h in (4, 5, 6, 7, 8)]
    recs.append({"rig": "piggyback", "target": "A", "filter": "OSC",
                 "hfr": 2.0, "background": 1000})
    ctx = q.night_context(recs)
    assert ctx[("rc16", "A", "Ha")]["hfr_median"] == 6
    assert ctx[("rc16", "A", "Ha")]["n_hfr"] == 5
    assert ctx[("piggyback", "A", "OSC")]["n_hfr"] == 1


def test_runs_sensor_temp_reasons_is_the_shared_rule():
    from photonscript.scheduler.runs import sensor_temp_reasons
    cfg = _cfg()
    assert sensor_temp_reasons(23.4, 20.0, cfg) == q.sensor_temp_reasons(
        23.4, 20.0, cfg)


def test_auto_approve_rc16_only_by_default():
    from photonscript.shared.config import PhotonScriptConfig
    from photonscript.shared.qa_rules import thresholds
    cfg = PhotonScriptConfig()
    assert thresholds(cfg, "rc16")["auto_approve"] is True
    assert thresholds(cfg, "piggyback")["auto_approve"] is False
    cfg.qa_auto_approve_rigs = ""
    assert thresholds(cfg, "piggyback")["auto_approve"] is True
