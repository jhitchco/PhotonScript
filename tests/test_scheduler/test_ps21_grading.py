"""PS-21: live and backfill grading share one scorecard (shared.qa_rules).

Parity: the telescope agent's record path and runs._fast_grade produce the
same scorecard from the same measurements; a rescore of a stored record
reproduces its card; manual verdicts keep the automatic result; the rescore
is dry-run by default and never touches human verdicts; both graders write
the PS-80 star sidecar.
"""

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest

from photonscript.shared import qa_rules as q
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (GuidingState, ImageQualityMetrics,
                                        TelescopeState)

NIGHT = "2026-09-26"


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                nina_logs_dir=str(tmp_path / "logs"),
                quality_eccentricity_max=0.6, quality_tracking_rms_max=2.0,
                stamp_fits_object=False, library_attribute=False)
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


def _write_light(path: Path, ccd_temp=0.3, n=8, sigma=2.6, size=420):
    from astropy.io import fits
    rng = np.random.default_rng(1)
    data = rng.normal(600, 9, (size, size)).astype(np.float32)
    yy, xx = np.mgrid[0:17, 0:17]
    g = np.exp(-((xx - 8) ** 2 + (yy - 8) ** 2) / (2 * sigma ** 2))
    step = (size - 40) // n
    for i, cy in enumerate(range(30, size - 30, step)):
        for j, cx in enumerate(range(30, size - 30, step)):
            amp = 3000.0 + 400.0 * ((i * n + j) % 7)
            data[cy - 8:cy + 9, cx - 8:cx + 9] += (amp * g).astype(np.float32)
    h = fits.Header()
    h["IMAGETYP"] = "LIGHT"
    h["EXPTIME"] = 300.0
    h["CCD-TEMP"] = ccd_temp
    h["SET-TEMP"] = 0.0
    h["FILTER"] = "Ha"
    h["OBJECT"] = "Test Nebula"
    h["DATE-OBS"] = "2026-09-27T04:00:00"
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data.astype(np.uint16), header=h).writeto(path,
                                                              overwrite=True)
    return path


class _Bus:
    def __init__(self):
        self.msgs = []

    async def publish(self, msg):
        self.msgs.append(msg)


def _agent(cfg, rig="rc16", temp=0.3, rms=0.0, gstate=GuidingState.STOPPED):
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config, a.rig, a.bus = cfg, rig, _Bus()
    a.state = TelescopeState()
    a.state.camera_temp_c = temp
    a.state.guiding.rms_total_arcsec = rms
    a.state.guiding.state = gstate
    a._consecutive_rejects, a._alerted = 0, set()

    async def _esc(*_a, **_k):
        pass
    a._escalate = _esc
    return a


def _need_sep():
    """The backfill grader needs sep for HFR/ecc and its star list."""
    try:
        import sep  # noqa: F401
    except ImportError:
        pytest.importorskip("sep_pjw")


def _records(cfg, night=NIGHT):
    from photonscript.scheduler.runs import _load_subs
    return _load_subs(cfg, night)


def _rescored(cfg, rec):
    k = q.group_key(rec)
    return q.evaluate(q.metrics_from_record(rec),
                      q.context(cfg, k[0], k[1], k[2])).compact()


# ------------------------------------------------ real frames, both graders

def test_live_record_carries_scorecard_and_rescore_reproduces_it(tmp_path):
    cfg = _cfg(tmp_path)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "a_Ha_300s_0001.fits")
    a = _agent(cfg, rms=98.0, gstate=GuidingState.GUIDING)
    asyncio.run(a._process_new_image(f))
    (rec,) = _records(cfg)
    for k in ("scorecard", "auto_verdict", "auto_reason", "drivers",
              "setpoint_c", "set_temp", "guide_rms", "guide_state", "noise",
              "clipped_pct", "sat_stars_pct", "swamp", "exposure", "qa_flag",
              "passed_qa", "reason"):
        assert k in rec, k
    assert rec["guide_state"] == "guiding" and rec["guide_rms"] == 98.0
    assert rec["setpoint_c"] == 0.0 and rec["set_temp"] == 0.0
    assert [r[0] for r in rec["scorecard"]["rows"]] == list(q.CHECKS)
    # the stored card is exactly what a rescore of the stored metrics gives
    assert _rescored(cfg, rec) == rec["scorecard"]
    assert rec["passed_qa"] == (rec["auto_verdict"] != "rejected")
    # the bus payload never carries the star table
    payload = a.bus.msgs[0].payload
    assert "star_table" not in json.dumps(payload)


def test_backfill_record_carries_scorecard_and_rescore_reproduces_it(tmp_path):
    from photonscript.scheduler.runs import _fast_grade
    _need_sep()
    cfg = _cfg(tmp_path)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "b_Ha_300s_0001.fits")
    rec = _fast_grade(f, cfg, [], stars_to=(NIGHT, "LIGHT/b_Ha_300s_0001.fits"))
    assert rec["rig"] == "rc16" and rec["graded_by"] == "sep-binned"
    assert rec["setpoint_c"] == 0.0 and rec["set_temp"] == 0.0
    assert _rescored(cfg, rec) == rec["scorecard"]
    fw = next(r for r in rec["scorecard"]["rows"] if r[0] == "fwhm")
    assert fw[3] == "skip"          # no true FWHM in this grader


def test_hot_sensor_rejected_the_same_way_by_both_graders(tmp_path):
    from photonscript.scheduler.runs import _fast_grade
    cfg = _cfg(tmp_path)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "h_Ha_300s_0001.fits",
                     ccd_temp=23.4)
    back = _fast_grade(f, cfg, [])
    asyncio.run(_agent(cfg, temp=23.4)._process_new_image(f))
    (live,) = _records(cfg)
    for rec in (back, live):
        assert rec["passed_qa"] is False and "temp" in rec["drivers"]
        assert "sensor 23C vs setpoint 0C (cooler failure)" in rec["reason"]


def test_both_graders_write_the_star_sidecar(tmp_path):
    from photonscript.scheduler.runs import _fast_grade
    from photonscript.shared import star_table
    _need_sep()
    cfg = _cfg(tmp_path)
    rel = "LIGHT/s_Ha_300s_0001.fits"
    f = _write_light(tmp_path / "fits" / NIGHT / rel)
    asyncio.run(_agent(cfg)._process_new_image(f))
    (live,) = _records(cfg)
    t = star_table.read(cfg, NIGHT, rel, "rc16")
    assert t and t["grader"] == "live-sep" and t["n"] >= 20
    assert t["w"] == 420 and t["h"] == 420
    assert set(t) >= {"x", "y", "hfr", "ecc", "theta"}
    assert abs(float(np.median(t["hfr"])) - live["hfr"]) < 0.05
    assert all(0 <= x <= 420 for x in t["x"])
    # backfill (binned x2 back to native px)
    star_table.sidecar_path(cfg, NIGHT, rel, "rc16").unlink()
    back = _fast_grade(f, cfg, [], stars_to=(NIGHT, rel))
    t = star_table.read(cfg, NIGHT, rel, "rc16")
    assert t and t["grader"] == "sep-binned" and t["w"] == 420
    assert abs(float(np.median(t["hfr"])) - back["hfr"]) < 0.1
    assert max(t["x"]) > 300          # native coordinates, not binned
    assert len(json.dumps(t)) < 40_000


def test_sidecar_off_when_limit_is_zero(tmp_path):
    from photonscript.shared import star_table
    cfg = _cfg(tmp_path, qa_star_sidecar_max=0)
    rel = "LIGHT/z_Ha_300s_0001.fits"
    f = _write_light(tmp_path / "fits" / NIGHT / rel)
    asyncio.run(_agent(cfg)._process_new_image(f))
    assert star_table.read(cfg, NIGHT, rel, "rc16") is None


# ------------------------------------ same metrics in, same scorecard out

METRICS = dict(hfr=6.2, ecc=0.58, stars=140, background=410.0, noise=11.0,
               clipped_pct=0.0, sat_stars_pct=0.0, swamp=9.0, exposure="ok")
# grader-specific inputs: FWHM (live only), tracking jump (backfill only),
# guiding (live only). Everything else must match exactly.
GRADER_SPECIFIC = {"fwhm", "tracking_jump", "guide_rms"}


@pytest.mark.parametrize("over", [{}, {"ecc": 0.66}, {"hfr": 11.0},
                                  {"stars": 3}, {"background": 257.0,
                                                 "hfr": 1.4, "stars": 12}])
def test_live_and_backfill_parity_on_the_same_metrics(tmp_path, monkeypatch,
                                                      over):
    from astropy.io import fits
    from photonscript.scheduler import runs
    from photonscript.telescope_agent import agent as agent_mod
    cfg = _cfg(tmp_path)
    mets = {**METRICS, **over}
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "p_Ha_300s_0001.fits",
                     ccd_temp=0.4, size=64, n=2)
    hdr = fits.getheader(f)

    def fake_validate(path, config, pixel_scale=None, rig="rc16"):
        return ImageQualityMetrics(
            hfr_pixels=mets["hfr"], fwhm_arcsec=None, star_count=mets["stars"],
            eccentricity=mets["ecc"], ecc_bin=mets["ecc"],
            background_adu=mets["background"],
            noise_adu=mets["noise"], clipped_pct=mets["clipped_pct"],
            sat_star_pct=mets["sat_stars_pct"], swamp_factor=mets["swamp"],
            exposure_flag=mets["exposure"])
    monkeypatch.setattr(agent_mod, "validate_image", fake_validate)
    monkeypatch.setattr(runs, "_load_binned",
                        lambda p: (hdr, np.zeros((8, 8), np.float32)))
    monkeypatch.setattr(runs, "_measure", lambda b, c: {
        **mets, "doubled_frac": None, "graded_by": "sep-binned",
        "_stars": None})
    # PS-94: no native re-measure here (it would read the real 64 px frame);
    # the backfill then keeps the binned value for both ecc and ecc_bin
    monkeypatch.setattr(runs, "_measure_native", lambda p: None)

    back = runs._fast_grade(f, cfg, [])
    asyncio.run(_agent(cfg, temp=0.4)._process_new_image(f))
    (live,) = _records(cfg)
    lrows = {r[0]: r for r in live["scorecard"]["rows"]}
    brows = {r[0]: r for r in back["scorecard"]["rows"]}
    assert set(lrows) == set(brows) == set(q.CHECKS)
    for cid in q.CHECKS:
        if cid in GRADER_SPECIFIC:
            continue
        assert lrows[cid] == brows[cid], cid
    for cid in GRADER_SPECIFIC:
        assert lrows[cid][3] == brows[cid][3] == "skip"
    for k in ("passed_qa", "reason", "qa_flag", "auto_verdict", "drivers"):
        assert live[k] == back[k], k


def test_live_rms_gate_now_sees_the_guiding_state(tmp_path, monkeypatch):
    """The old str(GuidingState) compare never matched ('guidingstate.
    guiding'), so the live RMS gate never ran. With the gate enabled
    (after PS-70), a guided sub over the limit is rejected."""
    from photonscript.telescope_agent import agent as agent_mod
    cfg = _cfg(tmp_path, qa_guide_rms_mode="fail")
    monkeypatch.setattr(agent_mod, "validate_image",
                        lambda *a, **k: ImageQualityMetrics(
                            hfr_pixels=5.0, star_count=100, eccentricity=0.3,
                            background_adu=400.0, exposure_flag="ok"))
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "g_Ha_300s_0001.fits",
                     size=64, n=2)
    asyncio.run(_agent(cfg, rms=2.6, gstate=GuidingState.GUIDING)
                ._process_new_image(f))
    (rec,) = _records(cfg)
    assert rec["drivers"] == ["guide_rms"] and not rec["passed_qa"]


def test_live_uses_the_night_median_so_far(tmp_path, monkeypatch):
    from photonscript.scheduler.runs import append_sub_record
    from photonscript.telescope_agent import agent as agent_mod
    cfg = _cfg(tmp_path)
    for i in range(6):
        append_sub_record(cfg, NIGHT, {
            "rig": "rc16", "file": f"LIGHT/old{i}.fits", "target": "Test Nebula",
            "filter": "Ha", "hfr": 5.0, "background": 400.0,
            "passed_qa": True, "reason": ""})
    monkeypatch.setattr(agent_mod, "validate_image",
                        lambda *a, **k: ImageQualityMetrics(
                            hfr_pixels=7.5, star_count=100, eccentricity=0.3,
                            background_adu=950.0, exposure_flag="ok"))
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "n_Ha_300s_0001.fits",
                     size=64, n=2)
    asyncio.run(_agent(cfg)._process_new_image(f))
    rec = _records(cfg)[-1]
    rows = {r[0]: r for r in rec["scorecard"]["rows"]}
    assert rows["hfr_rel"][3] == "fail" and rows["hfr_rel"][2] == 7.0
    assert rows["bg_rel"][3] == "warn"
    assert "HFR outlier" in rec["reason"]


# ------------------------------------------------------ verdicts / review

def _seed(cfg, recs):
    from photonscript.scheduler.runs import append_sub_record
    for r in recs:
        append_sub_record(cfg, NIGHT, r)


def _rec(name, **kw):
    r = {"rig": "rc16", "file": f"LIGHT/{name}.fits", "target": "T",
         "filter": "Ha", "exp_s": 300.0, "ccd_temp": 0.2, "hfr": 5.0,
         "fwhm_arcsec": 2.4, "stars": 150, "ecc": 0.4, "background": 400.0,
         "passed_qa": True, "reason": ""}
    r.update(kw)
    return r


def test_manual_verdict_keeps_the_auto_result(tmp_path):
    from photonscript.scheduler.runs import set_manual_qa
    cfg = _cfg(tmp_path)
    card = q.evaluate(q.metrics_from_record(_rec("x", ecc=0.66)),
                      q.context(cfg, "rc16"))
    _seed(cfg, [{**_rec("x", ecc=0.66), **card.record_fields()},
                _rec("legacy", passed_qa=False, reason="ecc 0.71")])
    hit = set_manual_qa(cfg, NIGHT, "LIGHT/x.fits", state="accepted")
    assert hit["passed_qa"] is True and hit["manual_qa"] and hit["reason"] == ""
    assert hit["auto_verdict"] == "rejected"
    assert hit["auto_reason"].startswith("Eccentricity 0.66")
    assert hit["drivers"] == ["ecc"] and hit["scorecard"]["verdict"] == "rejected"
    assert hit["review_source"] == "manual" and hit["reviewed_at"]
    hit = set_manual_qa(cfg, NIGHT, "LIGHT/x.fits", state="rejected", why="visual")
    assert hit["reason"] == "rejected manually: visual"
    assert hit["manual_reason"] == "visual"
    assert hit["auto_reason"].startswith("Eccentricity 0.66")
    # legacy record: the automatic reason is captured before it is overwritten
    hit = set_manual_qa(cfg, NIGHT, "LIGHT/legacy.fits", state="accepted")
    assert hit["auto_verdict"] == "rejected" and hit["auto_reason"] == "ecc 0.71"
    hit = set_manual_qa(cfg, NIGHT, "LIGHT/legacy.fits", state="rejected")
    assert hit["auto_reason"] == "ecc 0.71"       # not the manual text
    # sent back to review by a person: a rescore must not auto-approve it
    from photonscript.scheduler.runs import rescore_night
    set_manual_qa(cfg, NIGHT, "LIGHT/legacy.fits", state="review")
    rescore_night(cfg, NIGHT, apply=True, allow_unreject=True)
    back = {r["file"]: r for r in _records(cfg)}["LIGHT/legacy.fits"]
    assert back["reviewed"] is False and back["review_source"] == "manual"


def test_rescore_is_a_dry_run_by_default(tmp_path):
    from photonscript.scheduler.runs import rescore_night, runs_dir
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("a"), _rec("b", ecc=0.57), _rec("c", ecc=0.66)])
    p = runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    before = p.read_text()
    res = rescore_night(cfg, NIGHT)
    assert res["mode"] == "dry-run" and p.read_text() == before
    assert res["transitions"] == {"passed (legacy) -> approved": 1,
                                  "passed (legacy) -> needs-look": 1,
                                  "passed (legacy) -> rejected": 1}
    assert res["counts"]["newly_rejected"] == 1
    assert res["drivers"] == {"ecc": 1}


def test_rescore_apply_never_touches_human_verdicts(tmp_path):
    from photonscript.scheduler.runs import rescore_night
    cfg = _cfg(tmp_path)
    _seed(cfg, [
        _rec("auto_ok"),
        _rec("human_ok", ecc=0.66, reviewed=True, manual_qa=True),
        _rec("approved_night", ecc=0.66, reviewed=True),   # approve_night
        _rec("human_rej", passed_qa=False, reviewed=True, manual_qa=True,
             reason="rejected manually"),
        _rec("old_reject", passed_qa=False, reason="ecc 0.71"),
        _rec("now_bad", ecc=0.66)])
    res = rescore_night(cfg, NIGHT, apply=True)
    by = {r["file"].split("/")[1][:-5]: r for r in _records(cfg)}
    assert by["human_ok"]["passed_qa"] is True and "scorecard" not in by["human_ok"]
    assert by["approved_night"]["passed_qa"] is True
    assert by["human_rej"]["reason"] == "rejected manually"
    assert by["old_reject"]["passed_qa"] is False       # kept rejected
    assert by["old_reject"]["reason"] == "ecc 0.71"
    assert by["now_bad"]["passed_qa"] is False and by["now_bad"]["drivers"] == ["ecc"]
    assert by["auto_ok"]["reviewed"] is True
    assert by["auto_ok"]["review_source"] == "auto"
    assert res["counts"]["human_kept"] == 3
    assert res["counts"]["kept_rejected"] == 1
    # applying again changes nothing
    assert rescore_night(cfg, NIGHT, apply=True)["records_changed"] == 0


def test_rescore_unrejects_only_when_asked(tmp_path):
    from photonscript.scheduler.runs import rescore_night
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("old_reject", passed_qa=False, reason="ecc 0.71")])
    rescore_night(cfg, NIGHT, apply=True, allow_unreject=True)
    (r,) = _records(cfg)
    assert r["passed_qa"] is True and r["auto_verdict"] == "approved"


def test_rescore_offline_on_a_copy_refuses_apply(tmp_path):
    from photonscript.scheduler.runs import rescore_night
    cfg = _cfg(tmp_path)
    res = rescore_night(cfg, NIGHT, records=[_rec("a"), _rec("b", ecc=0.9)])
    assert res["subs"] == 2 and res["counts"]["newly_rejected"] == 1
    with pytest.raises(ValueError):
        rescore_night(cfg, NIGHT, apply=True, records=[_rec("a")])


def test_auto_approved_subs_reach_the_library(tmp_path):
    """All-green = approved: build_library links it like a reviewed sub."""
    from photonscript.scheduler.runs import build_library, library_root
    cfg = _cfg(tmp_path)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "L_Ha_300s_0001.fits")
    card = q.evaluate(q.metrics_from_record(_rec("L_Ha_300s_0001")),
                      q.context(cfg, "rc16"))
    assert card.auto_approved
    _seed(cfg, [{**_rec("L_Ha_300s_0001", target="Test Nebula",
                        abs_path=str(f)), **card.record_fields()}])
    res = build_library(cfg, NIGHT)
    assert res.get("linked", 0) == 1
    assert list(library_root(cfg).rglob("L_Ha_300s_0001.fits"))


# ---------------------------------------------------------------- API

def test_api_exposes_the_scorecard(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from photonscript.scheduler.routers import review
    from photonscript.scheduler.runs import night_detail
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    monkeypatch.setattr(app, "_config", cfg)
    card = q.evaluate(q.metrics_from_record(_rec("new", ecc=0.66)),
                      q.context(cfg, "rc16"))
    _seed(cfg, [{**_rec("new", ecc=0.66), **card.record_fields()},
                _rec("legacy")])
    d = night_detail(cfg, NIGHT, backfill=False)
    by = {s["file"]: s for s in d["subs"]}
    assert by["LIGHT/new.fits"]["scorecard"]["verdict"] == "rejected"
    assert "scored_on_read" not in by["LIGHT/new.fits"]
    assert by["LIGHT/legacy.fits"]["scored_on_read"] is True
    assert by["LIGHT/legacy.fits"]["scorecard"]["verdict"] == "approved"
    assert by["LIGHT/legacy.fits"]["passed_qa"] is True
    out = review.api_sub_scorecard(NIGHT, file="LIGHT/new.fits")
    assert out["verdict"] == "rejected" and out["checks"][0]["id"] == "ecc"
    assert out["checks"][0]["status"] == "fail"
    assert out["checks"][0]["limit"] == 0.6 and out["thresholds"]["ecc_max"] == 0.6
    assert len(out["checks"]) == len(q.CHECKS)
    out = review.api_sub_scorecard(NIGHT, file="LIGHT/legacy.fits")
    assert out["scored_on_read"] is True
    assert review.api_sub_scorecard(NIGHT, file="nope").status_code == 404
    th = review.api_qa_thresholds(rig="piggyback")
    assert th["thresholds"]["hfr_max"] == 4.5
    assert [c["id"] for c in th["checks"]] == list(q.CHECKS)
    pv = review.api_rescore_preview(NIGHT)
    assert pv["mode"] == "dry-run"
    assert review.api_sub_stars(NIGHT, file="LIGHT/new.fits").status_code == 404
    # mounted on the app (served over HTTP, not only importable)
    from fastapi.testclient import TestClient
    client = TestClient(app.app)
    r = client.get("/api/qa/thresholds", params={"rig": "piggyback"})
    assert r.status_code == 200 and r.json()["thresholds"]["ecc_max"] == 0.75
    r = client.get(f"/api/runs/{NIGHT}/scorecard",
                   params={"file": "LIGHT/new.fits"})
    assert r.status_code == 200
    assert r.json()["verdict"] == "rejected"
    r = client.get(f"/api/runs/{NIGHT}/rescore")
    assert r.status_code == 200 and r.json()["mode"] == "dry-run"
    r = client.get(f"/api/runs/{NIGHT}/stars", params={"file": "x.fits"})
    assert r.status_code == 404


# ------------------------------------------- PS-94: formula + binned scale

def test_ps94_old_backfill_records_convert_to_sqrt_form(tmp_path):
    """A pre-PS-94 'sep-binned' record holds 1-b/a: 0.20 is the same star as
    sqrt-form 0.60. Live records and records with ecc_def are left alone."""
    old = _rec("old", ecc=0.20, graded_by="sep-binned")
    assert q.metrics_from_record(old)["ecc"] == pytest.approx(0.60)
    assert q.record_ecc(old) == pytest.approx(0.60)
    live = _rec("live", ecc=0.20)
    assert q.metrics_from_record(live)["ecc"] == 0.20
    new = _rec("new", ecc=0.20, graded_by="sep-binned",
               ecc_def="sqrt(1-(b/a)^2)")
    assert q.metrics_from_record(new)["ecc"] == 0.20
    assert q.record_ecc(_rec("n", ecc=None, graded_by="sep-binned")) is None


def test_ps94_rescore_tightens_old_backfill_records_dry_run(tmp_path):
    """lin 0.25 (b/a 0.75) passed the 0.60 gate before; in sqrt form it is
    0.66 and fails. Dry run: nothing is written."""
    from photonscript.scheduler.runs import rescore_night, runs_dir
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("old", ecc=0.25, graded_by="sep-binned"),
                _rec("live", ecc=0.25)])
    p = runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    before = p.read_text()
    res = rescore_night(cfg, NIGHT)
    assert p.read_text() == before
    assert res["counts"]["newly_rejected"] == 1
    (d,) = [x for x in res["diffs"] if x["action"] == "rejected"]
    assert d["file"] == "LIGHT/old.fits" and d["drivers"] == ["ecc"]


def test_ps94_native_gate_by_default_binned_info_only(tmp_path):
    cfg = _cfg(tmp_path)
    t = q.thresholds(cfg, "rc16")
    assert t["ecc_scale"] == "native" and t["ecc_max_bin"] == 0.6
    card = q.evaluate({"ecc": 0.66, "ecc_bin": 0.55, "stars": 100},
                      q.context(cfg, "rc16"))
    rows = {c.id: c for c in card.checks}
    assert rows["ecc"].status == "fail" and rows["ecc_bin"].status == "skip"
    assert rows["ecc_bin"].value == 0.55
    assert card.drivers == ["ecc"]
    # the compact row keeps the value; expand() explains the skip
    exp = {r["id"]: r for r in q.expand(card.compact())}
    assert "info only" in exp["ecc_bin"]["reason"]


def test_ps94_binned_gate_when_chosen(tmp_path):
    cfg = _cfg(tmp_path, qa_ecc_scale="binned",
               quality_eccentricity_max_binned=0.55)
    t = q.thresholds(cfg, "rc16")
    assert t["ecc_scale"] == "binned" and t["ecc_max_bin"] == 0.55
    card = q.evaluate({"ecc": 0.66, "ecc_bin": 0.45, "stars": 100},
                      q.context(cfg, "rc16"))
    rows = {c.id: c for c in card.checks}
    assert rows["ecc"].status == "skip" and rows["ecc"].value == 0.66
    assert rows["ecc_bin"].status == "pass" and "ecc" not in card.drivers
    card = q.evaluate({"ecc": 0.40, "ecc_bin": 0.58, "stars": 100},
                      q.context(cfg, "rc16"))
    assert card.drivers == ["ecc_bin"]
    assert card.reason.startswith("Eccentricity at 0.48\"/px 0.58 > 0.55")
    # no binned value (Piggy-600, older record): native gates anyway
    card = q.evaluate({"ecc": 0.66, "stars": 100}, q.context(cfg, "rc16"))
    assert card.drivers == ["ecc"]
    # gating_ecc follows the same choice
    assert q.gating_ecc({"ecc": 0.66, "ecc_bin": 0.5}, t) == (0.5, 0.55)
    assert q.gating_ecc({"ecc": 0.66}, t) == (0.66, 0.6)
    assert q.gating_ecc({"ecc": 0.66, "ecc_bin": 0.5},
                        q.thresholds(_cfg(tmp_path), "rc16")) == (0.66, 0.6)


def test_ps94_bad_scale_setting_falls_back_to_native(tmp_path):
    assert q.thresholds(_cfg(tmp_path, qa_ecc_scale="Bin2"),
                        "rc16")["ecc_scale"] == "native"


def test_ps94_backfill_records_both_scales_in_sqrt_form(tmp_path):
    from photonscript.scheduler.runs import _fast_grade
    from photonscript.shared import star_table
    _need_sep()
    cfg = _cfg(tmp_path)
    rel = "LIGHT/e_Ha_300s_0001.fits"
    f = _write_light(tmp_path / "fits" / NIGHT / rel, sigma=3.4)
    rec = _fast_grade(f, cfg, [], stars_to=(NIGHT, rel))
    assert rec["ecc_def"] == "sqrt(1-(b/a)^2)" and rec["ecc_at"] == "native"
    assert rec["ecc"] is not None and rec["ecc_bin"] is not None
    assert rec["hfr_bin"] == rec["hfr"]
    # round synthetic stars read round at both scales
    assert rec["ecc"] < 0.25 and rec["ecc_bin"] < 0.3
    assert _rescored(cfg, rec) == rec["scorecard"]
    assert star_table.read(cfg, NIGHT, rel, "rc16")["ecc_def"] == \
        "sqrt(1-(b/a)^2)"
    # native=False (or a MemoryError) keeps the binned value as ecc
    rec2 = _fast_grade(f, cfg, [], native=False)
    assert rec2["ecc"] == rec2["ecc_bin"] and rec2["ecc_at"] == "binned"


def test_ps94_backfill_survives_memory_error_on_native(tmp_path, monkeypatch):
    from photonscript.scheduler import runs
    _need_sep()
    cfg = _cfg(tmp_path)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "m_Ha_300s_0001.fits")

    def boom(p):
        raise MemoryError
    monkeypatch.setattr(runs, "_load_native", boom)
    rec = runs._fast_grade(f, cfg, [])
    assert rec["ecc_at"] == "binned" and rec["ecc"] == rec["ecc_bin"]


def test_ps94_live_record_carries_ecc_bin(tmp_path):
    _need_sep()
    cfg = _cfg(tmp_path)
    f = _write_light(tmp_path / "fits" / NIGHT / "LIGHT" / "k_Ha_300s_0001.fits",
                     sigma=3.4)
    asyncio.run(_agent(cfg)._process_new_image(f))
    (rec,) = _records(cfg)
    assert rec["ecc_def"] == "sqrt(1-(b/a)^2)"
    assert rec["ecc_bin"] is not None and rec["hfr_bin"] is not None
    rows = {r[0]: r for r in rec["scorecard"]["rows"]}
    assert rows["ecc_bin"][3] == "skip" and rows["ecc_bin"][1] == rec["ecc_bin"]
    assert _rescored(cfg, rec) == rec["scorecard"]


def test_ps94_runs_page_shows_old_records_in_sqrt_form(tmp_path):
    from photonscript.scheduler.runs import night_detail
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec("old", ecc=0.2, graded_by="sep-binned"),
                _rec("live", ecc=0.2)])
    d = night_detail(cfg, NIGHT, backfill=False)
    by = {s["file"]: s for s in d["subs"]}
    assert by["LIGHT/old.fits"]["ecc"] == 0.6
    assert by["LIGHT/old.fits"]["ecc_raw"] == 0.2
    assert by["LIGHT/live.fits"]["ecc"] == 0.2
    assert "ecc_raw" not in by["LIGHT/live.fits"]


def test_ps94_config_fields_exposed():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    c = PhotonScriptConfig(_env_file=None)
    assert by_env["PS_QA_ECC_SCALE"][4] == "str" and c.qa_ecc_scale == "native"
    assert by_env["PS_QA_ECC_BINNED"][4] == "bool" and c.qa_ecc_binned is True
    assert by_env["PS_QUALITY_ECCENTRICITY_MAX_BINNED"][4] == "float"
    assert c.quality_eccentricity_max_binned == 0.0
