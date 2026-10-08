"""PS-114: QA gates per rig and the data-driven baselines.

Every gate resolves per rig through one mechanism (rigs.PIGGYBACK_GATES:
the RC16 key, a Piggy-600 override key); the RC16 keeps today's values;
the Piggy-600's FWHM gate fits its own stars, so a normal 11.3" Piggy sub
no longer loses points while the same FWHM still fails on the RC16; the
side panel shows the rig's gate; qa-baselines proposes gates from the
accepted subs and never changes one; a rescore preview counts the scores
the new gates move.
"""

import json

import pytest

from photonscript.shared import qa_rules as q
from photonscript.shared import qa_score as qs
from photonscript.shared.config import PhotonScriptConfig
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed

PIGGY_GOOD = dict(hfr=2.6, fwhm_arcsec=8.0, ecc=0.35, stars=400,
                  background=800.0, exp_s=120.0, ccd_temp=0.1, exposure="ok",
                  sat_stars_pct=0.0, clipped_pct=0.0)
RC16_GOOD = dict(hfr=5.0, fwhm_arcsec=2.4, ecc=0.4, stars=150,
                 background=400.0, exp_s=300.0, ccd_temp=0.2, exposure="ok",
                 sat_stars_pct=0.0, clipped_pct=0.0)


def _c(**kw):
    base = dict(_env_file=None, quality_eccentricity_max=0.6,
                piggyback_enabled=True)
    base.update(kw)
    return PhotonScriptConfig(**base)


def _card(cfg, rig, **m):
    base = dict(PIGGY_GOOD if rig == "piggyback" else RC16_GOOD)
    base.update(m)
    return q.evaluate(q.record_metrics(**base),
                      q.context(cfg, rig, "M 31", "OSC" if rig == "piggyback"
                                else "Ha"))


def _row(card, cid):
    return next(c for c in card.checks if c.id == cid)


# ------------------------------------------------------- per-rig resolution

def test_rc16_keeps_todays_gates():
    t = q.thresholds(_c(), "rc16")
    assert t["fwhm_max"] == 4.0 and t["fwhm_soft"] is False
    assert t["hfr_max"] == 10.0 and t["ecc_max"] == 0.6
    assert (t["star_min"], t["star_max"]) == (5, 5000)
    assert t["bg_rel_max"] == 2.0 and t["hfr_rel_factor"] == 1.4
    assert t["doubled_max"] == 0.25 and t["guide_rms_max"] == 1.5
    assert t["corner_spread_max"] == 0.35 and t["bias_floor"] == 256 + 6.0
    # the live scope PC sets PS_QUALITY_FWHM_MAX=6: still the RC16's alone
    cfg = _c(quality_fwhm_max=6.0)
    assert q.thresholds(cfg, "rc16")["fwhm_max"] == 6.0
    assert q.thresholds(cfg, "piggyback")["fwhm_max"] == 15.0


def test_piggy_overrides_apply_and_unset_ones_follow_the_rc16():
    cfg = _c(quality_star_min=12, qa_background_rel_max=1.8)
    pb = q.thresholds(cfg, "piggyback")
    assert pb["fwhm_max"] == 15.0 and pb["fwhm_soft"] is True
    assert pb["hfr_max"] == 4.5 and pb["ecc_max"] == 0.75
    assert pb["star_min"] == 12 and pb["bg_rel_max"] == 1.8   # blank: RC16's
    cfg = _c(quality_star_min=12, piggyback_star_min=3,
             piggyback_background_rel_max=2.5, piggyback_fwhm_max=13.0,
             piggyback_hfr_outlier_factor=1.6, piggyback_tracking_jump_max=0.3,
             piggyback_tracking_rms_max=4.0, piggyback_corner_spread_max=0.5,
             piggyback_bias_floor_margin_adu=10.0, piggyback_star_max=900)
    rc, pb = q.thresholds(cfg, "rc16"), q.thresholds(cfg, "piggyback")
    assert rc["star_min"] == 12 and pb["star_min"] == 3
    assert (rc["bg_rel_max"], pb["bg_rel_max"]) == (2.0, 2.5)
    assert pb["fwhm_max"] == 13.0 and pb["hfr_rel_factor"] == 1.6
    assert pb["doubled_max"] == 0.3 and pb["guide_rms_max"] == 4.0
    assert pb["corner_spread_max"] == 0.5 and pb["star_max"] == 900
    assert pb["bias_floor"] == 256 + 10.0 and rc["bias_floor"] == 256 + 6.0
    assert rc["hfr_rel_factor"] == 1.4 and rc["doubled_max"] == 0.25


def test_piggy_override_from_the_env(monkeypatch):
    monkeypatch.setenv("PS_PIGGYBACK_STAR_MIN", "40")
    monkeypatch.setenv("PS_PIGGYBACK_FWHM_MAX", "14")
    cfg = PhotonScriptConfig(_env_file=None)
    assert q.thresholds(cfg, "piggyback")["star_min"] == 40
    assert q.thresholds(cfg, "piggyback")["fwhm_max"] == 14.0
    assert q.thresholds(cfg, "rc16")["star_min"] == 5


def test_every_gate_row_has_a_piggy_key_and_a_config_field():
    from photonscript.shared.rigs import PIGGYBACK_GATES, gate_key
    from photonscript.scheduler.app import _CONFIG_FIELDS
    fields = {f[0] for f in _CONFIG_FIELDS}
    c = PhotonScriptConfig(_env_file=None)
    for base, pb, _ in PIGGYBACK_GATES:
        assert hasattr(c, base) and hasattr(c, pb), (base, pb)
        assert pb in fields and base in fields, (base, pb)
        assert gate_key("piggyback", base) == pb and gate_key("rc16", base) == base
    g = q.rig_gates(_c())
    assert [r["id"] for r in g["rigs"]] == ["rc16", "piggyback"]
    by = {r["gate"]: r for r in g["rows"]}
    assert by["fwhm_max"]["rc16"] == 4.0 and by["fwhm_max"]["piggyback"] == 15.0
    assert by["fwhm_max"]["piggyback_env"] == "PS_PIGGYBACK_FWHM_MAX"
    assert by["star_min"]["piggyback_key"] == "piggyback_star_min"
    assert by["setpoint_c"]["piggyback_key"] == "piggyback_setpoint_c"
    assert set(by) == {gid for gid, *_ in q.GATES}


# ------------------------------------------------------------ FWHM grading

def test_piggy_11_3_arcsec_fwhm_no_longer_deducts():
    """Jeremy 2026-10-04: "FWHM 11.3\" > 6\" (advisory on this rig)", -4.9."""
    old = _card(_c(piggyback_fwhm_max=6.0), "piggyback", fwhm_arcsec=11.3)
    assert _row(old, "fwhm").status == "warn"
    assert "fwhm" in [d[0] for d in old.score.deductions]
    new = _card(_c(), "piggyback", fwhm_arcsec=11.3)
    assert _row(new, "fwhm").status == "pass" and _row(new, "fwhm").limit == 15.0
    assert "fwhm" not in [d[0] for d in new.score.deductions]
    assert new.score.value == 100
    # the whole normal 8 to 12" range is free
    for f in (8.0, 10.0, 12.0):
        assert _card(_c(), "piggyback", fwhm_arcsec=f).score.value == 100


def test_rc16_11_3_arcsec_fwhm_still_fails():
    for cfg in (_c(), _c(quality_fwhm_max=6.0)):
        card = _card(cfg, "rc16", fwhm_arcsec=11.3)
        assert _row(card, "fwhm").status == "fail"
        assert card.verdict == "rejected"
        assert "fwhm" in [d[0] for d in card.score.deductions]


# ---------------------------------------------------------------- panel

def test_panel_shows_the_rigs_gate_and_flags_a_stale_card():
    old = _card(_c(piggyback_fwhm_max=6.0), "piggyback", fwhm_arcsec=11.3)
    t_now = q.thresholds(_c(), "piggyback")
    rows = {r["id"]: r for r in qs.panel_rows(
        {}, q.expand(old.compact()), t_now, old.score.as_dict())}
    assert rows["fwhm"]["gate"] == "<= 15\""
    assert "graded against 6\"" in rows["fwhm"]["note"]
    assert rows["hfr"]["gate"] == "<= 4.5 px" and "graded" not in rows["hfr"]["note"]
    rc = _card(_c(), "rc16")
    rows = {r["id"]: r for r in qs.panel_rows(
        {}, q.expand(rc.compact()), q.thresholds(_c(), "rc16"))}
    assert rows["fwhm"]["gate"] == "<= 4\"" and rows["fwhm"]["note"] == ""


# ------------------------------------------------------------- baselines

def _recs():
    """Synthetic accepted subs: 25 Piggy-600 OSC (FWHM 8 to 12"), 25 RC16
    Ha, and rejects that must not count."""
    out = []
    for i in range(25):
        out.append({"rig": "piggyback", "file": f"p{i}.fits", "target": "M 31",
                    "filter": "OSC", "_night": "2026-10-0%d" % (1 + i % 3),
                    "fwhm_arcsec": 8.0 + (i % 5), "hfr": 2.5 + 0.1 * (i % 5),
                    "ecc": 0.30 + 0.02 * (i % 5), "stars": 400,
                    "background": 800.0 + 10 * (i % 5), "passed_qa": True})
        out.append({"rig": "rc16", "file": f"r{i}.fits", "target": "M 31",
                    "filter": "Ha", "_night": "2026-10-0%d" % (1 + i % 3),
                    "fwhm_arcsec": 2.0 + 0.2 * (i % 5), "hfr": 4.0 + 0.5 * (i % 5),
                    "ecc": 0.40 + 0.02 * (i % 5), "stars": 150 + 10 * (i % 5),
                    "background": 300.0, "passed_qa": True})
    out.append({"rig": "piggyback", "file": "bad.fits", "target": "M 31",
                "filter": "OSC", "_night": "2026-10-01", "fwhm_arcsec": 40.0,
                "hfr": 9.0, "stars": 12, "background": 5000.0,
                "passed_qa": False})
    return out


def test_baselines_median_mad_and_proposals():
    from photonscript.scheduler.qa_baselines import MAD_SIGMA, baselines
    rep = baselines(_c(), records=_recs())
    assert rep["k"] == 3.0
    rigs = {r["rig"]: r for r in rep["rigs"]}
    pb = {f["filter"]: f for f in rigs["piggyback"]["filters"]}
    assert set(pb) == {"*", "OSC"} and pb["*"]["subs"] == 25   # reject left out
    fw = pb["*"]["metrics"]["fwhm"]
    assert fw["n"] == 25 and fw["median"] == 10.0 and fw["mad"] == 1.0
    assert fw["proposed"] == round(10.0 + 3 * MAD_SIGMA, 1) == 14.4
    assert fw["current"] == 15.0 and fw["gate_key"] == "fwhm_max"
    st = pb["*"]["metrics"]["stars"]
    assert st["median"] == 400 and st["proposed"] == 100   # a quarter
    assert pb["*"]["metrics"]["background"]["proposed"] is None   # info only
    assert pb["*"]["metrics"]["hfr_ratio"]["n"] == 25
    rc = {f["filter"]: f for f in rigs["rc16"]["filters"]}["*"]["metrics"]
    assert rc["fwhm"]["median"] == 2.4 and rc["fwhm"]["current"] == 4.0
    assert rc["hfr"]["current"] == 10.0
    # one rig, a bigger k, and too few subs for a proposal
    rep = baselines(_c(), rig="rc16", k=2.0, records=_recs())
    assert [r["rig"] for r in rep["rigs"]] == ["rc16"] and rep["k"] == 2.0
    rep = baselines(_c(), records=_recs()[:10])
    assert all(m["proposed"] is None for r in rep["rigs"] for f in r["filters"]
               for m in f["metrics"].values())


def test_baselines_never_change_a_gate():
    from photonscript.scheduler.qa_baselines import baselines
    cfg = _c()
    before = (q.thresholds(cfg, "rc16"), q.thresholds(cfg, "piggyback"))
    baselines(cfg, records=_recs())
    assert (q.thresholds(cfg, "rc16"), q.thresholds(cfg, "piggyback")) == before


def test_backfill_fwhm_is_not_a_measured_fwhm():
    from photonscript.scheduler.qa_baselines import baselines
    recs = [dict(r, graded_by="sep-binned") for r in _recs()]
    rep = baselines(_c(), records=recs)
    for r in rep["rigs"]:
        assert r["filters"][0]["metrics"]["fwhm"]["n"] == 0


def test_qa_baselines_cli_on_synthetic_records(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript.cli import app
    d = tmp_path / "runs"
    d.mkdir()
    by_night: dict = {}
    for r in _recs():
        by_night.setdefault(r["_night"], []).append(
            {k: v for k, v in r.items() if k != "_night"})
    for night, rs in by_night.items():
        (d / f"{night}_subs.jsonl").write_text(
            "".join(json.dumps(r) + "\n" for r in rs), encoding="utf-8")
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path / "data"))
    r = CliRunner().invoke(app, ["qa-baselines", "--records", str(d), "--json",
                                 "--rig", "piggyback"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    (pb,) = out["rigs"]
    allf = pb["filters"][0]
    assert allf["filter"] == "*" and allf["nights"] == 3
    assert allf["metrics"]["fwhm"]["proposed"] == 14.4
    r = CliRunner().invoke(app, ["qa-baselines", "--records", str(d),
                                 "--nights", "2"])
    assert r.exit_code == 0, r.output
    assert "Piggy-600 (piggyback) filter *" in r.output
    assert "fwhm_max" in r.output and "Report only" in r.output
    assert r.output.isascii()


def test_baselines_from_the_runs_logs_and_the_api(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient
    from photonscript.scheduler.qa_baselines import baselines
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    from photonscript.scheduler.runs import append_sub_record
    for r in _recs():
        night = r.pop("_night")
        append_sub_record(cfg, night, r)
    rep = baselines(cfg, nights=2)                  # 10-03 and 10-02 only
    pb = next(x for x in rep["rigs"] if x["rig"] == "piggyback")
    assert pb["filters"][0]["nights"] == 2 and pb["filters"][0]["subs"] == 16
    monkeypatch.setattr(app, "_config", cfg)
    client = TestClient(app.app)
    out = client.get("/api/qa/baselines", params={"nights": 14}).json()
    assert {r["rig"] for r in out["rigs"]} == {"rc16", "piggyback"}
    g = client.get("/api/qa/gates").json()
    assert {r["gate"] for r in g["rows"]} >= {"fwhm_max", "star_min"}


# ------------------------------------------------------- rescore preview

def test_rescore_preview_counts_the_scores_the_new_gate_moves(tmp_path):
    """Piggy-600 subs graded under the old 6" FWHM gate: the dry run counts
    the scores that move (all up), changes no verdict and writes nothing."""
    from photonscript.scheduler.runs import _load_subs, rescore_night
    old_cfg = _cfg(tmp_path, piggyback_enabled=True, piggyback_fwhm_max=6.0)
    recs = []
    for i, f in enumerate((11.3, 9.0, 7.0, 4.0)):
        r = _rec(f"p{i}", rig="piggyback", filter="OSC", hfr=2.6, stars=400,
                 fwhm_arcsec=f, ecc=0.35, background=800.0, exp_s=120.0,
                 ccd_temp=0.1)
        k = q.group_key(r)
        r.update(q.evaluate(q.metrics_from_record(r),
                            q.context(old_cfg, *k)).record_fields())
        recs.append(r)
    assert [r["score"] < 100 for r in recs] == [True, True, True, False]
    rc = _rec("r0", fwhm_arcsec=11.3)          # RC16: fails before and after
    k = q.group_key(rc)
    rc.update(q.evaluate(q.metrics_from_record(rc),
                         q.context(old_cfg, *k)).record_fields())
    _seed(old_cfg, recs + [rc])
    new_cfg = _cfg(tmp_path, piggyback_enabled=True)
    before = [dict(r) for r in _load_subs(new_cfg, NIGHT)]
    dry = rescore_night(new_cfg, NIGHT)
    assert dry["mode"] == "dry-run" and dry["records_changed"] == 0
    assert dry["counts"]["score_changed"] == 3
    assert dry["counts"]["score_up"] == 3 and "score_down" not in dry["counts"]
    assert dry["counts"]["new_rejected"] == 1        # the RC16 11.3" sub
    assert _load_subs(new_cfg, NIGHT) == before


@pytest.mark.parametrize("rig", ["rc16", "piggyback"])
def test_every_grader_entry_resolves_the_rig(rig):
    """context() and thresholds() give the same gates for a rig, so the
    live grader, backfill, rescore and the score all see the rig's gate."""
    cfg = _c()
    ctx = q.context(cfg, rig, "M 31", "OSC")
    assert ctx.thresholds == q.thresholds(cfg, rig, "M 31", "OSC")
    assert ctx.thresholds["rig"] == rig
