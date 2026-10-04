"""PS-108: the 0 to 100 sub score end to end.

Both graders record the new pixel counts (same function, same numbers);
the stored score survives a rescore; qa-rescore writes the score onto every
sub (human verdicts and kept rejects too) without changing a verdict in
preview; in "on" mode it sets the verdict but never touches a human one;
the per-night score report and its CLI; the API side panel and histogram.
"""

import asyncio
import json

import numpy as np
import pytest

from photonscript.shared import qa_rules as q
from tests.test_scheduler.test_ps21_grading import (NIGHT, _agent, _cfg,
                                                    _need_sep, _rec, _records,
                                                    _seed, _write_light)


def _light_with_clipping(path):
    """A small synthetic light with 6 saturated and 3 black pixels."""
    from astropy.io import fits
    _write_light(path)
    with fits.open(path) as h:
        data = h[0].data.astype(np.int32)
        hdr = h[0].header.copy()
    data[2, 2:8] = 65535
    data[400, 1:4] = 0
    fits.PrimaryHDU(data.astype(np.uint16), header=hdr).writeto(path, overwrite=True)
    return path


def test_both_graders_record_the_same_pixel_counts(tmp_path):
    from photonscript.scheduler.runs import _fast_grade
    _need_sep()
    cfg = _cfg(tmp_path)
    f = _light_with_clipping(tmp_path / "fits" / NIGHT / "LIGHT" / "c_Ha_300s_0001.fits")
    back = _fast_grade(f, cfg, [])
    asyncio.run(_agent(cfg)._process_new_image(f))
    (live,) = _records(cfg)
    for k in ("sat_px", "sat_px_pct", "zero_px", "zero_px_pct", "max_adu",
              "sat_adu", "bg_median", "bg_mad"):
        assert live[k] == back[k], k
    assert live["sat_px"] == 6 and live["zero_px"] == 3
    assert live["max_adu"] == 65535.0 and live["sat_adu"] == 65000.0
    # the score is stored on both records and a rescore reproduces it
    for rec in (live, back):
        assert rec["score"] == rec["scorecard"]["score"]["s"]
        k = q.group_key(rec)
        again = q.evaluate(q.metrics_from_record(rec),
                           q.context(cfg, k[0], k[1], k[2]))
        assert again.score.value == rec["score"]


def _scored(cfg, name, rig="rc16", drop_score=False, **kw):
    r = _rec(name, rig=rig, **kw)
    k = q.group_key(r)
    card = q.evaluate(q.metrics_from_record(r), q.context(cfg, k[0], k[1], k[2]))
    r.update(card.record_fields())
    if drop_score:     # a record graded before PS-108
        r.pop("score", None)
        r.pop("score_decision", None)
        r["scorecard"].pop("score", None)
    return r


def test_rescore_preview_writes_scores_everywhere_and_no_verdict(tmp_path):
    from photonscript.scheduler.runs import rescore_night
    cfg = _cfg(tmp_path)
    human = _scored(cfg, "human", drop_score=True, ecc=0.9)
    human.update(passed_qa=True, reviewed=True, manual_qa=True,
                 review_source="manual")
    kept = _scored(cfg, "kept", drop_score=True, ecc=0.62)      # rejected today
    auto = _scored(cfg, "auto", drop_score=True)                 # auto-approved
    _seed(cfg, [human, kept, auto])
    before = {r["file"]: (r["passed_qa"], r.get("reviewed")) for r in _records(cfg)}
    dry = rescore_night(cfg, NIGHT)
    assert dry["score_mode"] == "preview" and dry["records_changed"] == 0
    assert dry["counts"]["score_approve"] == 1
    assert dry["counts"]["score_review"] == 1 and dry["counts"]["score_reject"] == 1
    assert all("score" not in r for r in _records(cfg))
    res = rescore_night(cfg, NIGHT, apply=True)
    assert res["records_changed"] == 3
    by = {r["file"]: r for r in _records(cfg)}
    assert by["LIGHT/human.fits"]["score_decision"] == "reject"
    assert by["LIGHT/kept.fits"]["score_decision"] == "review"
    assert by["LIGHT/auto.fits"]["score"] == 100
    assert by["LIGHT/kept.fits"]["scorecard"]["score"]["s"] == by["LIGHT/kept.fits"]["score"]
    assert {f: (r["passed_qa"], r.get("reviewed")) for f, r in by.items()} == before
    # idempotent
    assert rescore_night(cfg, NIGHT, apply=True)["records_changed"] == 0


def test_rescore_on_mode_applies_the_score_but_never_a_human_verdict(tmp_path):
    from photonscript.scheduler.runs import rescore_night
    cfg = _cfg(tmp_path)
    human = _scored(cfg, "human", ecc=0.9)
    human.update(passed_qa=True, reviewed=True, manual_qa=True,
                 review_source="manual")
    was_green = _scored(cfg, "green")            # auto-approved by PS-21
    assert was_green["review_source"] == "auto"
    was_green["ecc"] = 0.62                      # now just over the gate
    pig = _scored(cfg, "pig", rig="piggyback", filter="OSC", hfr=3.0)
    assert not pig.get("reviewed")               # Piggy-600 manual today
    _seed(cfg, [human, was_green, pig])
    on = _cfg(tmp_path, qa_score_mode="on")
    rescore_night(on, NIGHT, apply=True)
    by = {r["file"]: r for r in _records(on)}
    h = by["LIGHT/human.fits"]
    assert h["passed_qa"] is True and h["reviewed"] is True and h["manual_qa"]
    g = by["LIGHT/green.fits"]
    assert g["passed_qa"] is True and not g.get("reviewed")   # back to review
    assert g["score_decision"] == "review" and g["reason"] == ""
    p = by["LIGHT/pig.fits"]
    assert p["reviewed"] is True and p["review_source"] == "auto"


def test_score_report_counts_and_moves(tmp_path):
    from photonscript.scheduler.runs import format_score_report, score_report
    cfg = _cfg(tmp_path)
    a = _scored(cfg, "a")                                  # approved, stays
    b = _scored(cfg, "b")
    b.update(reviewed=False, review_source=None)           # review -> approve
    c = _scored(cfg, "c", ecc=0.62)                        # rejected -> review
    d = _scored(cfg, "d", ecc=0.9)                         # rejected, stays
    e = _scored(cfg, "e", ecc=0.9)
    e.update(passed_qa=True, reviewed=True, manual_qa=True,
             review_source="manual")                       # human, kept
    rep = score_report(cfg, NIGHT, records=[a, b, c, d, e])
    assert rep["mode"] == "preview" and rep["subs"] == 5
    assert rep["today"] == {"approved": 3, "review": 0, "rejected": 2} or \
        rep["today"] == {"approved": 2, "review": 1, "rejected": 2}
    assert rep["today"]["rejected"] == 2
    assert rep["score"] == {"approve": 2, "review": 1, "reject": 2}
    assert rep["moves"] == {"review -> approved": 1, "rejected -> review": 1}
    assert rep["would_move"] == 2
    assert rep["human"] == {"n": 1, "disagree": 1}
    assert rep["by_rig"]["rc16"]["would_move"] == 2
    assert sum(rep["histogram"]) == 5
    text = format_score_report(rep)
    assert "would move: 2" in text and "human verdicts kept: 1" in text
    assert text.isascii()


def test_score_report_cli(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript.cli import app
    cfg = _cfg(tmp_path)
    recs = [_scored(cfg, "a"), _scored(cfg, "c", ecc=0.62)]
    p = tmp_path / "copy.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PS_QUALITY_ECCENTRICITY_MAX", "0.6")
    r = CliRunner().invoke(app, ["score-report", "--date", NIGHT,
                                 "--records", str(p), "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["moves"] == {"rejected -> review": 1}
    r = CliRunner().invoke(app, ["score-report", "--date", NIGHT,
                                 "--records", str(p)])
    assert r.exit_code == 0 and "by score: 1 approve / 1 review / 0 reject" in r.output


def test_api_panel_score_report_and_histogram(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    monkeypatch.setattr(app, "_config", cfg)
    f = _light_with_clipping(tmp_path / "fits" / NIGHT / "LIGHT" / "new.fits")
    new = _scored(cfg, "new", ecc=0.62, abs_path=str(f))
    legacy = _rec("legacy")                     # graded before PS-21 / PS-108
    _seed(cfg, [new, legacy])
    client = TestClient(app.app)
    r = client.get(f"/api/runs/{NIGHT}/scorecard", params={"file": "LIGHT/new.fits"})
    assert r.status_code == 200
    out = r.json()
    assert out["score"]["score"] == new["score"] and out["score"]["mode"] == "preview"
    assert out["score"]["decision"] == "review" and out["today"] == "rejected"
    ids = [m["id"] for m in out["panel"]]
    for k in ("stars", "hfr", "fwhm", "ecc", "ecc_bin", "corner_spread",
              "background", "sat_px", "zero_px", "max_adu", "sat_stars",
              "swamp", "temp", "guide_rms", "pointing", "slew_straddle", "roof"):
        assert k in ids, k
    ecc = next(m for m in out["panel"] if m["id"] == "ecc")
    assert ecc["status"] == "red" and ecc["lost"]
    r = client.get(f"/api/runs/{NIGHT}/scorecard", params={"file": "LIGHT/legacy.fits"})
    assert r.json()["score"]["on_read"] is True and r.json()["score"]["score"] == 100
    r = client.get(f"/api/runs/{NIGHT}/score-report")
    assert r.status_code == 200 and r.json()["subs"] == 2
    r = client.get(f"/api/runs/{NIGHT}/hist", params={"file": "LIGHT/new.fits"})
    assert r.status_code == 200
    h = r.json()
    assert h["channels"]["L"]["n"] == 420 * 420
    assert h["white_clip_pct"] == pytest.approx(100 * 6 / 420 ** 2, abs=1e-3)
    assert (tmp_path / "data" / "hist" / NIGHT / "LIGHT_new.fits.json").exists()
    r = client.get(f"/api/runs/{NIGHT}/hist", params={"file": "LIGHT/legacy.fits"})
    assert r.status_code == 404


def test_config_fields_on_the_system_page():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    c = PhotonScriptConfig(_env_file=None)
    for env, typ, default in (("PS_QA_SCORE_MODE", "str", "preview"),
                              ("PS_QA_SCORE_APPROVE", "float", 80.0),
                              ("PS_QA_SCORE_REJECT", "float", 60.0),
                              ("PS_QA_SATURATION_ADU", "float", 65000.0)):
        assert by_env[env][4] == typ and by_env[env][3] == "Quality"
        assert getattr(c, by_env[env][0]) == default
    assert q.RULES_VERSION == "ps21.3"
