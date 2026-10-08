"""PS-130: qa-rescore --remeasure re-measures pre-PS-83 backfill records
(graded_by, no measure_v) from their FITS with shared.star_measure, then
re-judges the night with rescore_night (PS-21 scorecard, PS-108 score).

Human verdicts (manual accept / reject, approve_night, sent back to review)
keep every field a person set; missing FITS are skipped and counted; the dry
run writes nothing and reports auto verdict changes only.
"""

import json
import time
from pathlib import Path

import numpy as np
import pytest

from photonscript.shared import qa_rules as q
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-09-26"


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                nina_logs_dir=str(tmp_path / "logs"),
                quality_eccentricity_max=0.6, pixel_scale_arcsec=0.24,
                stamp_fits_object=False, library_attribute=False,
                guard_enabled=False)
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


def _old(name, **kw):
    """A pre-PS-83 backfill record: binned HFR ~2x live, HFR x scale as FWHM,
    1-b/a ecc (no ecc_def), no measure_v."""
    r = {"rig": "rc16", "file": f"LIGHT/{name}.fits", "target": "T",
         "filter": "Ha", "exp_s": 300.0, "ccd_temp": 0.2, "set_temp": 0.0,
         "hfr": 14.0, "fwhm_arcsec": 3.36, "stars": 900, "ecc": 0.3,
         "background": 400.0, "graded_by": "sep-binned",
         "passed_qa": True, "reason": ""}
    r.update(kw)
    return r


def _graded(cfg, rec):
    """The record as the old grader stored it (scorecard + verdict fields)."""
    card = q.evaluate(q.metrics_from_record(rec), q.context(cfg, "rc16"))
    return {**rec, **card.record_fields()}


def _seed(cfg, recs, files=True):
    from photonscript.scheduler.runs import append_sub_record
    for r in recs:
        append_sub_record(cfg, NIGHT, r)
        if files and r.get("file") and "missing" not in r["file"]:
            p = Path(cfg.image_watch_dir) / NIGHT / r["file"]
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(b"")          # the fake measure never reads it


# what the fake measure returns per file (the PS-83 numbers)
NEW = {"hfr": 7.0, "fwhm_arcsec": 1.9, "stars": 400, "ecc": 0.42,
       "ecc_bin": 0.40, "hfr_bin": 3.6, "ecc_at": "native",
       "ecc_def": "sqrt(1-(b/a)^2)", "measure_v": "ps83.1",
       "background": 401.0, "noise": 12.0, "corner_spread": 0.05,
       "clipped_pct": 0.0, "sat_stars_pct": 0.5, "swamp": 9.0,
       "exposure": "ok", "graded_by": "backfill-sep",
       # PS-146: the measure says what its judged ecc / FWHM came from (a
       # record without these gets them derived by the rescore)
       "ecc_src": "all", "fwhm_src": "moment"}


def _fake(per_file=None, calls=None):
    per_file = per_file or {}

    def measure(config, path, rig="rc16"):
        if calls is not None:
            calls.append(Path(path).name)
        if "broken" in Path(path).name:
            raise OSError("bad FITS")
        return {**NEW, **per_file.get(Path(path).stem, {})}, None
    return measure


def _night(cfg):
    from photonscript.scheduler.runs import _load_subs
    return {r["file"].split("/")[-1][:-5]: r for r in _load_subs(cfg, NIGHT)}


def _fixture(cfg):
    """auto subs, a manually approved, a manually rejected, one approved via
    approve_night, one sent back to review by hand, a missing FITS, a live
    record and a PS-83 record (neither re-measured)."""
    _seed(cfg, [
        _graded(cfg, _old("auto_ok")),
        _graded(cfg, _old("auto_ok2")),
        _graded(cfg, _old("auto_bad")),          # new ecc fails
        {**_graded(cfg, _old("human_ok", ecc=0.5)), "passed_qa": True,
         "reviewed": True, "manual_qa": True, "review_source": "manual",
         "reviewed_at": "2026-09-27T15:00:00Z", "reason": ""},
        {**_graded(cfg, _old("human_rej")), "passed_qa": False,
         "reviewed": True, "manual_qa": True, "review_source": "manual",
         "reviewed_at": "2026-09-27T15:01:00Z", "manual_reason": "visual",
         "reason": "rejected manually: visual"},
        {**_old("approved_night"), "reviewed": True},
        {**_graded(cfg, _old("human_review")), "passed_qa": True,
         "reviewed": False, "manual_qa": False, "review_source": "manual",
         "reviewed_at": "2026-09-27T15:02:00Z"},
        _graded(cfg, _old("missing_one")),
        {"rig": "rc16", "file": "LIGHT/live.fits", "target": "T",
         "filter": "Ha", "exp_s": 300.0, "hfr": 6.8, "fwhm_arcsec": 1.8,
         "stars": 380, "ecc": 0.4, "background": 400.0, "passed_qa": True,
         "reason": "", "ecc_def": "sqrt(1-(b/a)^2)"},
        {**_old("ps83"), "hfr": 7.1, "measure_v": "ps83.1",
         "graded_by": "backfill-sep", "ecc_def": "sqrt(1-(b/a)^2)"},
    ])


PER_FILE = {"auto_bad": {"ecc": 0.75},
            # a person accepted it; the new measure would reject it
            "human_ok": {"ecc": 0.8},
            # a person rejected it; the new measure passes everything
            "human_rej": {"ecc": 0.3}}

HUMAN = ("human_ok", "human_rej", "approved_night", "human_review")


# ------------------------------------------------------------------ core

def test_needs_remeasure_only_pre_ps83_backfill():
    from photonscript.scheduler.qa_remeasure import needs_remeasure
    assert needs_remeasure(_old("a"))
    assert not needs_remeasure({**_old("a"), "measure_v": "ps83.1"})
    assert not needs_remeasure({"hfr": 7.0})            # live record


def test_dry_run_writes_nothing_and_reports_auto_changes(tmp_path):
    from photonscript.scheduler import qa_remeasure as rm
    from photonscript.scheduler.runs import runs_dir
    cfg = _cfg(tmp_path)
    _fixture(cfg)
    p = runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    before = p.read_text()
    calls = []
    res = rm.remeasure_night(cfg, NIGHT, measure=_fake(PER_FILE, calls))
    assert p.read_text() == before
    assert res["mode"] == "dry-run"
    assert res["subs"] == 10 and res["pre_ps83"] == 8
    assert res["remeasured"] == 7 and res["missing_fits"] == 1
    assert res["missing"] == ["LIGHT/missing_one.fits"]
    assert "live.fits" not in calls and "ps83.fits" not in calls
    assert res["hfr_median"] == {"old": 14.0, "new": 7.0}
    assert "warning" in res              # mixed old / new HFR in the median
    files = {d["file"] for d in res["verdict_changes"]}
    assert "LIGHT/auto_bad.fits" in files
    assert not files & {f"LIGHT/{h}.fits" for h in HUMAN}
    assert res["human_kept"] == 4
    bad = next(d for d in res["verdict_changes"]
               if d["file"] == "LIGHT/auto_bad.fits")
    assert bad["new"] == "rejected" and "ecc" in bad["drivers"]
    # PS-108: the score is part of the re-judge
    assert any(k.startswith("score_") for k in res["counts"])


def test_apply_updates_metrics_and_keeps_every_human_field(tmp_path):
    from photonscript.scheduler import qa_remeasure as rm
    cfg = _cfg(tmp_path)
    _fixture(cfg)
    before = _night(cfg)
    res = rm.remeasure_night(cfg, NIGHT, apply=True, allow_unreject=True,
                             measure=_fake(PER_FILE))
    assert res["mode"] == "apply" and res["records_written"] == 7
    after = _night(cfg)
    for name in HUMAN:
        for k in rm.MANUAL_FIELDS:
            assert after[name].get(k) == before[name].get(k), (name, k)
    # the manually rejected sub stays rejected even with --allow-unreject
    assert after["human_rej"]["passed_qa"] is False
    assert after["human_rej"]["reason"] == "rejected manually: visual"
    assert after["human_rej"]["manual_reason"] == "visual"
    # the manually approved sub stays approved although ecc 0.8 now fails
    assert after["human_ok"]["passed_qa"] is True
    assert after["human_ok"]["reviewed"] is True
    # sent back to review by hand: not auto-approved by the rescore
    assert after["human_review"]["reviewed"] is False
    # metrics, measure_v and the audit trail
    for name in ("auto_ok", "auto_bad", *HUMAN):
        r = after[name]
        assert r["measure_v"] == "ps83.1" and r["remeasured"] == "ps83.1"
        assert r["pre_ps83"]["hfr"] == 14.0
        assert r["pre_ps83"]["graded_by"] == "sep-binned"
        assert r["hfr"] == 7.0 and r["fwhm_arcsec"] == 1.9
        assert r["ecc_def"] == "sqrt(1-(b/a)^2)"
    # the scorecard rows describe the new numbers (side panel), human too
    rows = {x["id"]: x for x in q.expand(after["human_ok"]["scorecard"])}
    assert rows["ecc"]["status"] == "fail"
    assert rows["fwhm"]["status"] != "skip"     # FWHM is judged now
    # PS-108: the score follows the new numbers (ecc 0.8 fails hard)
    assert isinstance(after["human_ok"].get("score"), (int, float))
    assert after["human_ok"]["score"] < 80
    # auto verdicts move
    assert after["auto_bad"]["passed_qa"] is False
    assert after["auto_bad"]["drivers"] == ["ecc"]
    assert after["auto_ok"]["passed_qa"] is True
    # not re-measured: missing FITS, live and PS-83 records (the rescore
    # still re-judges them with the night, as qa-rescore does)
    for name in ("missing_one", "live", "ps83"):
        for k in ("hfr", "stars", "ecc", "measure_v", "graded_by"):
            assert after[name].get(k) == before[name].get(k), (name, k)
        assert "pre_ps83" not in after[name]
    # PS-146: the rescore gives the live and PS-83 records the robust FWHM
    # from their stored numbers (moment 1.8" under 1.2 x HFR: 2 x HFR now),
    # the moment value kept; the pre-PS-83 record is left to the remeasure
    assert after["missing_one"]["fwhm_arcsec"] == \
        before["missing_one"]["fwhm_arcsec"]
    assert "ecc_src" not in after["missing_one"]
    for name in ("live", "ps83"):
        assert after[name]["fwhm_moment_arcsec"] == \
            before[name]["fwhm_arcsec"]
        assert after[name]["shape_from"] == "stored"
        assert after[name]["fwhm_src"] in ("moment", "hfr")
    # the stale binned HFR now stands out against the fresh night median
    assert "HFR outlier" in after["missing_one"]["reason"]
    # a second run has nothing left but the missing FITS
    again = rm.remeasure_night(cfg, NIGHT, apply=True,
                               measure=_fake(PER_FILE))
    assert again["pre_ps83"] == 1 and again["missing_fits"] == 1
    assert again["remeasured"] == 0


def test_failed_measure_is_skipped_and_counted(tmp_path):
    from photonscript.scheduler import qa_remeasure as rm
    cfg = _cfg(tmp_path)
    _seed(cfg, [_graded(cfg, _old("ok")), _graded(cfg, _old("broken"))])
    res = rm.remeasure_night(cfg, NIGHT, apply=True, measure=_fake())
    assert res["failed"] == 1 and res["remeasured"] == 1
    assert res["errors"][0]["file"] == "LIGHT/broken.fits"
    n = _night(cfg)
    assert "measure_v" not in n["broken"] and n["ok"]["measure_v"]


def test_apply_merges_onto_records_appended_meanwhile(tmp_path):
    """The log is re-read before the write: a sub appended while the FITS
    were measured is kept."""
    from photonscript.scheduler import qa_remeasure as rm
    from photonscript.scheduler.runs import append_sub_record
    cfg = _cfg(tmp_path)
    _seed(cfg, [_graded(cfg, _old("a"))])
    inner = _fake()

    def measure(config, path, rig="rc16"):
        append_sub_record(cfg, NIGHT, {"rig": "rc16", "file": "LIGHT/new.fits",
                                       "hfr": 6.9, "passed_qa": True})
        return inner(config, path, rig)
    rm.remeasure_night(cfg, NIGHT, apply=True, measure=measure)
    n = _night(cfg)
    assert set(n) == {"a", "new"} and n["a"]["measure_v"] == "ps83.1"


def test_all_before_ps83_finds_only_old_nights(tmp_path):
    from photonscript.scheduler import qa_remeasure as rm
    from photonscript.scheduler.runs import append_sub_record
    cfg = _cfg(tmp_path)
    _seed(cfg, [_graded(cfg, _old("a"))])
    append_sub_record(cfg, "2026-10-05", {**_old("b"), "measure_v": "ps83.1"})
    assert rm.nights_before_ps83(cfg) == [NIGHT]
    rep = rm.remeasure(cfg, None, measure=_fake())
    assert [n["date"] for n in rep["nights"]] == [NIGHT]
    assert rep["totals"]["remeasured"] == 1 and rep["mode"] == "dry-run"
    text = rm.format_report(rep)
    assert NIGHT in text and "HFR median 14.0 -> 7.0" in text
    text.encode("ascii")


def test_fits_path_falls_back_to_the_night_folder(tmp_path):
    from photonscript.scheduler.qa_remeasure import fits_path
    cfg = _cfg(tmp_path)
    f = Path(cfg.image_watch_dir) / NIGHT / "LIGHT" / "x.fits"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"")
    rec = {"file": "LIGHT/x.fits", "abs_path": "Z:/gone/x.fits"}
    assert fits_path(cfg, NIGHT, rec) == f
    assert fits_path(cfg, NIGHT, {"file": "LIGHT/y.fits"}) is None


# ------------------------------------------------- real FITS, real measure

def _need_sep():
    try:
        import sep  # noqa: F401
    except ImportError:
        pytest.importorskip("sep_pjw")


def _write_light(path: Path, n=8, sigma=3.4, size=420):
    from astropy.io import fits
    rng = np.random.default_rng(1)
    data = rng.normal(600, 9, (size, size)).astype(np.float32)
    yy, xx = np.mgrid[0:21, 0:21]
    g = np.exp(-((xx - 10) ** 2 + (yy - 10) ** 2) / (2 * sigma ** 2))
    step = (size - 40) // n
    for i, cy in enumerate(range(30, size - 30, step)):
        for j, cx in enumerate(range(30, size - 30, step)):
            amp = 3000.0 + 400.0 * ((i * n + j) % 7)
            data[cy - 10:cy + 11, cx - 10:cx + 11] += (amp * g).astype(
                np.float32)
    h = fits.Header()
    h["IMAGETYP"] = "LIGHT"
    h["EXPTIME"] = 300.0
    h["FILTER"] = "Ha"
    h["OBJECT"] = "T"
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(data.astype(np.uint16), header=h).writeto(path,
                                                              overwrite=True)
    return path


def test_real_measure_matches_measure_frame(tmp_path):
    _need_sep()
    from photonscript.scheduler import qa_remeasure as rm
    from photonscript.shared import star_measure as sm
    from photonscript.shared.rigs import rig_config
    cfg = _cfg(tmp_path)
    f = _write_light(Path(cfg.image_watch_dir) / NIGHT / "LIGHT" / "r.fits")
    fields, table = rm.measure_sub(cfg, f, "rc16")
    from astropy.io import fits
    # the header too: measure_sub passes it (read noise, PS-117 sky rate)
    ref = sm.measure_frame(sm.load_native(f), rig_config(cfg, "rc16"),
                           "rc16", grader="backfill-sep",
                           header=fits.getheader(f))
    for mk, rk in rm.MEASURED:
        assert fields[rk] == ref[mk], rk
    assert fields["measure_v"] == sm.MEASURE_VERSION
    assert fields["ecc_at"] == "native" and fields["stars"] > 0
    # end to end on the record: a pre-PS-83 approved-by-hand sub
    _seed(cfg, [{**_graded(cfg, _old("r")), "abs_path": str(f),
                 "passed_qa": True, "reason": "", "reviewed": True,
                 "manual_qa": True, "review_source": "manual"}],
          files=False)
    res = rm.remeasure_night(cfg, NIGHT, apply=True)
    assert res["remeasured"] == 1
    r = _night(cfg)["r"]
    assert r["hfr"] == ref["hfr"] and r["measure_v"] == sm.MEASURE_VERSION
    assert r["manual_qa"] is True and r["passed_qa"] is True


# ------------------------------------------------------------- CLI + API

def test_cli_remeasure(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    from photonscript.scheduler import qa_remeasure as rm
    import photonscript.shared.config as config_mod
    cfg = _cfg(tmp_path)
    _fixture(cfg)
    monkeypatch.setattr(config_mod, "PhotonScriptConfig", lambda: cfg)
    monkeypatch.setattr(rm, "measure_sub", _fake(PER_FILE))
    run = CliRunner().invoke
    r = run(cli.app, ["qa-rescore", "--remeasure", "--date", NIGHT,
                      "--dry-run"])
    assert r.exit_code == 0, r.output
    assert "1 FITS missing" in r.output and "auto_bad" in r.output
    assert "human_ok" not in r.output
    assert "measure_v" not in json.dumps(_night(cfg)["auto_ok"])
    r = run(cli.app, ["qa-rescore", "--remeasure", "--all-before-ps83",
                      "--apply"])
    assert r.exit_code == 0, r.output
    assert _night(cfg)["auto_ok"]["measure_v"] == "ps83.1"
    # bad combinations
    for args in (["--remeasure", "--date", NIGHT, "--apply", "--dry-run"],
                 ["--all-before-ps83", "--date", NIGHT],
                 ["--remeasure"],
                 ["--remeasure", "--date", NIGHT, "--all-before-ps83"],
                 []):
        assert run(cli.app, ["qa-rescore", *args]).exit_code != 0, args


def test_api_remeasure_runs_in_the_background(tmp_path, monkeypatch):
    import photonscript.scheduler.app as app
    from fastapi.testclient import TestClient
    from photonscript.scheduler import ecc_scale
    from photonscript.scheduler import qa_remeasure as rm
    cfg = _cfg(tmp_path)
    _fixture(cfg)
    monkeypatch.setattr(app, "_config", cfg)
    monkeypatch.setattr(rm, "measure_sub", _fake(PER_FILE))
    monkeypatch.setattr(ecc_scale, "blockers", lambda config=None: [])
    rm._jobs.clear()
    client = TestClient(app.app)
    assert client.get(f"/api/runs/{NIGHT}/remeasure").json()["status"] == "idle"
    r = client.post(f"/api/runs/{NIGHT}/remeasure", json={})
    assert r.status_code == 202 and r.json()["apply"] is False
    for _ in range(100):
        st = client.get(f"/api/runs/{NIGHT}/remeasure").json()
        if st["status"] != "running":
            break
        time.sleep(0.05)
    assert st["status"] == "done", st
    assert st["result"]["remeasured"] == 7
    assert st["result"]["mode"] == "dry-run"
    assert "measure_v" not in _night(cfg)["auto_ok"]
    # refused while the armer is armed
    monkeypatch.setattr(ecc_scale, "blockers",
                        lambda config=None: ["armer is ARMED: run the "
                                             "ecc-scale report in daytime"])
    r = client.post(f"/api/runs/{NIGHT}/remeasure", json={"apply": True})
    assert r.status_code == 409 and "re-measure in daytime" in r.json()["detail"]
    rm._jobs.clear()


def test_binned_hfr_rejects_come_back_only_with_allow_unreject(tmp_path):
    """An auto reject driven by the old binned HFR (14 px > 10) passes on the
    new measure, but the PS-21 kept-reject rule holds without
    --allow-unreject (reported as kept_rejected)."""
    from photonscript.scheduler import qa_remeasure as rm
    cfg = _cfg(tmp_path)
    _seed(cfg, [_graded(cfg, _old("soft", ecc=0.1))])
    assert _night(cfg)["soft"]["passed_qa"] is False
    res = rm.remeasure_night(cfg, NIGHT, apply=True, measure=_fake())
    assert res["counts"].get("kept_rejected") == 1
    n = _night(cfg)["soft"]
    assert n["passed_qa"] is False and n["hfr"] == 7.0
    # the stored card shows the new numbers pass
    assert n["scorecard"]["verdict"] != "rejected"
    # a fresh pre-PS-83 copy with --allow-unreject comes back
    _seed(cfg, [_graded(cfg, _old("soft2", ecc=0.1))])
    rm.remeasure_night(cfg, NIGHT, apply=True, allow_unreject=True,
                       measure=_fake())
    assert _night(cfg)["soft2"]["passed_qa"] is True


def test_remeasure_passes_the_frame_header_for_read_noise(monkeypatch, tmp_path):
    """PS-117 x PS-130: the re-measure hands each frame's header to the
    measure, so an LCG RC16 frame gets the LCG read noise in swamp."""
    from pathlib import Path
    from photonscript.scheduler import qa_remeasure, runs
    seen = {}

    def fake_native(path, config, rig="rc16", osc=False, header=None):
        seen["header"] = header
        return {"hfr": 2.0}
    monkeypatch.setattr(runs, "_measure_native", fake_native)
    import astropy.io.fits as fits
    import numpy as np
    p = Path(tmp_path) / "x.fits"
    h = fits.Header()
    h["READOUTM"] = "Low Conversion Gain"
    fits.writeto(p, np.zeros((8, 8), dtype=np.uint16), h)
    from photonscript.shared.config import PhotonScriptConfig
    qa_remeasure.measure_sub(PhotonScriptConfig(), p, "rc16")
    assert seen["header"] is not None
    assert seen["header"]["READOUTM"] == "Low Conversion Gain"
