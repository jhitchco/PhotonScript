"""Target core saturation (2026-10-13, M31): per-sub core metrics in both
graders (shared.core_sat), the scorecard "core" row, the HDR coverage of a
clipped core (scheduler.core_check), the integration master check
(integration.core_master) and the core-check CLI."""

import json
import math
import struct
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from photonscript.shared import core_sat as cs
from photonscript.shared import qa_rules as q
from photonscript.shared.config import PhotonScriptConfig
from tests.test_integration.test_ps177_178_exposure_groups import mono_lib  # noqa: F401

NIGHT = "2026-10-12"
M31_RA, M31_DEC = 0.712 * 15.0, 41.27      # the catalog row (shared.astronomy)
SIZE = 600
SCALE = 1.0                                 # "/px: a 1' core is 60 px


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                nina_logs_dir=str(tmp_path / "logs"),
                pixel_scale_arcsec=SCALE, stamp_fits_object=False,
                library_attribute=False, guard_enabled=False)
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


def _frame(core_amp=120000.0, core_xy=(330, 280), core_sigma=14.0,
           sat_star_xy=(60, 60), seed=4):
    """Sky + a few stars + one saturated star far from the core + a
    Gaussian galaxy core (amp above 65535 clips it) as physical ADU."""
    rng = np.random.default_rng(seed)
    d = rng.normal(800, 12, (SIZE, SIZE))
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    cx, cy = core_xy
    d += core_amp * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * core_sigma ** 2))
    # a broad bulge so the core sits on galaxy light
    d += 3000 * np.exp(-((xx - cx) ** 2 + (yy - cy) ** 2) / (2 * 120.0 ** 2))
    for i in range(12):
        sx, sy = rng.integers(20, SIZE - 20, 2)
        d += 4000 * np.exp(-((xx - sx) ** 2 + (yy - sy) ** 2) / (2 * 2.0 ** 2))
    sx, sy = sat_star_xy
    d += 200000 * np.exp(-((xx - sx) ** 2 + (yy - sy) ** 2) / (2 * 2.5 ** 2))
    return np.clip(d, 0, 65535)


def _write(path: Path, data, *, wcs=False, header_pos=True, obj="M31",
           filt="L", exp=300.0):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = "LIGHT"
    h["EXPTIME"] = exp
    h["FILTER"] = filt
    h["OBJECT"] = obj
    h["DATE-OBS"] = "2026-10-13T05:00:00"
    h["OFFSET"] = 256
    if header_pos:
        h["RA"] = M31_RA
        h["DEC"] = M31_DEC
    if wcs:
        # target at array (x 330, y 280); +y north, +x west (sky view)
        s = SCALE / 3600.0
        h["CRVAL1"] = M31_RA
        h["CRVAL2"] = M31_DEC
        h["CRPIX1"] = 331.0
        h["CRPIX2"] = 281.0
        h["CD1_1"] = -s
        h["CD1_2"] = 0.0
        h["CD2_1"] = 0.0
        h["CD2_2"] = s
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.asarray(data).astype(np.uint16), header=h).writeto(
        path, overwrite=True)
    return path


# ------------------------------------------------------------ per sub

def test_saturated_core_found_from_header_pointing(tmp_path):
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "a.fits", _frame())
    c = cs.sub_core(cfg, "rc16", f, None, "M31")
    assert c["core_src"] == "header"
    assert c["core_sat_px"] > 100                  # the clipped plateau
    assert c["core_peak_adu"] >= 65000
    assert c["core_p999"] >= 65000
    assert c["core_sat_adu"] == 65000.0
    # the search walked to the core (off center by (30, -20) px)
    x, y = c["core_xy"]
    assert abs(x - 330) < 15 and abs(y - 280) < 15


def test_saturated_star_outside_core_not_counted(tmp_path):
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "b.fits", _frame(core_amp=20000.0))
    c = cs.sub_core(cfg, "rc16", f, None, "M31")
    assert c["core_sat_px"] == 0                   # only the far star clips
    assert 15000 < c["core_peak_adu"] < 30000
    assert c["core_p999"] < 65000


def test_saturated_star_inside_the_disk_is_not_the_core(tmp_path):
    cfg = _cfg(tmp_path)
    # unclipped nucleus at (330, 280), a saturated star 40 px away (inside
    # the 60 px disk, outside the 15 px nucleus)
    f = _write(tmp_path / "s.fits", _frame(core_amp=20000.0, sat_star_xy=(370, 280)))
    c = cs.sub_core(cfg, "rc16", f, None, "M31")
    assert c["core_sat_px"] == 0 and c["core_sat_px_disk"] > 0
    assert c["core_peak_adu"] < 65000


def test_wcs_places_core_without_search(tmp_path):
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "c.fits", _frame(), wcs=True)
    c = cs.sub_core(cfg, "rc16", f, None, "M31")
    assert c["core_src"] == "wcs"
    assert c["core_xy"][0] == pytest.approx(330, abs=16)
    assert c["core_xy"][1] == pytest.approx(280, abs=16)
    assert c["core_sat_px"] > 100


def test_stored_solve_used(tmp_path):
    from photonscript.scheduler.solve_store import nominal_cd, store_path
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "d.fits", _frame(core_xy=(300, 300)), header_pos=False)
    p = store_path(cfg, NIGHT, "rc16")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"file": "LIGHT/d.fits", "rig": "rc16", "solved": True,
                             "ra": M31_RA, "dec": M31_DEC,
                             "cd": nominal_cd(SCALE)}) + "\n", encoding="utf-8")
    c = cs.sub_core(cfg, "rc16", f, None, "M31", night=NIGHT, rel_file="LIGHT/d.fits")
    assert c["core_src"] == "solve"
    assert c["core_sat_px"] > 100


def test_no_target_mode_off_and_far_target(tmp_path):
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "e.fits", _frame())
    assert cs.sub_core(cfg, "rc16", f, None, "?") == {}
    assert cs.sub_core(_cfg(tmp_path, qa_core_mode="off"), "rc16", f, None, "M31") == {}
    # header pointing 5 deg away: the target is not in a 10' frame
    from astropy.io import fits
    with fits.open(f, mode="update") as h:
        h[0].header["DEC"] = M31_DEC + 5.0
    assert cs.sub_core(cfg, "rc16", f, None, "M31") == {}


def test_radius_from_catalog():
    cfg = SimpleNamespace(qa_core_radius_arcmin=1.0)
    assert cs.radius_arcmin(cfg, "M31") == 1.0                 # galaxy
    assert cs.radius_arcmin(cfg, "Orion Nebula") == 3.0        # 10% of 85', capped 3x
    assert cs.radius_arcmin(cfg, "Unknown thing") == 1.0


def test_backfill_grader_records_core(tmp_path):
    from photonscript.scheduler.runs import _fast_grade
    pytest.importorskip("sep")
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "m31_L_300s_0001.fits", _frame())
    rec = _fast_grade(f, cfg, ["M31"], native=False)
    assert rec["core_sat_px"] > 100 and rec["core_src"] == "header"
    rows = {r[0]: r for r in rec["scorecard"]["rows"]}
    assert [r[0] for r in rec["scorecard"]["rows"]] == list(q.CHECKS)
    assert rows["core"][3] == q.PASS                 # note mode: never held
    assert "target core clipped" in rows["core"][4]
    assert "core" not in rec["drivers"]


# ------------------------------------------------------------ scorecard

def _card(cfg, **m):
    return q.evaluate(q.record_metrics(**m), q.context(cfg, "rc16", "M31", "L"))


def test_core_row_note_warn_off(tmp_path):
    cfg = _cfg(tmp_path)
    c = _card(cfg, core_sat_px=400, core_peak_adu=65535)
    row = next(x for x in c.checks if x.id == "core")
    assert row.status == q.PASS and "400 px" in row.reason and row.limit == 25
    assert c.verdict != q.REJECTED and "core" not in c.warnings
    assert next(x for x in _card(cfg, core_sat_px=10).checks if x.id == "core").reason == ""
    w = _card(_cfg(tmp_path, qa_core_mode="warn"), core_sat_px=400)
    assert "core" in w.warnings and w.passed and "core" not in w.drivers
    assert next(x for x in _card(_cfg(tmp_path, qa_core_mode="warn"),
                                 core_sat_px=25).checks if x.id == "core").status == q.PASS
    off = _card(_cfg(tmp_path, qa_core_mode="off"), core_sat_px=400)
    assert next(x for x in off.checks if x.id == "core").status == q.SKIP
    none = _card(cfg)
    assert next(x for x in none.checks if x.id == "core").status == q.SKIP
    # a stored record rescores the same row
    rec = {"core_sat_px": 400, "core_peak_adu": 65535}
    assert q.metrics_from_record(rec)["core_sat_px"] == 400


def test_core_row_has_no_score_weight(tmp_path):
    cfg = _cfg(tmp_path)
    a = _card(cfg, core_sat_px=0, hfr=2.0, stars=200)
    b = _card(cfg, core_sat_px=5000, hfr=2.0, stars=200)
    assert a.score.value == b.score.value


# ------------------------------------------------------------ coverage

def _row(exp, sat_px, p999=None, verdict="approved", flt="L", date=NIGHT):
    return {"date": date, "target": "Andromeda Galaxy", "rig": "rc16", "filter": flt,
            "exp_s": exp, "verdict": verdict, "core_sat_px": sat_px,
            "core_peak_adu": 65535 if sat_px and sat_px > 25 else 30000,
            "core_p999": p999 if p999 is not None else (65535 if sat_px > 25 else 30000),
            "sat_adu": 65000.0, "offset": 256}


def _proj(short_s=None, count=0, flt="L"):
    return SimpleNamespace(exposure_plans=[SimpleNamespace(
        filter_type=SimpleNamespace(value=flt), rig="rc16",
        hdr_short_seconds=short_s, hdr_short_count=count)])


def test_coverage_no_plan(tmp_path):
    from photonscript.scheduler import core_check as cc
    cfg = _cfg(tmp_path)
    rows = [_row(300, 900) for _ in range(5)]
    c = cc.coverage(cfg, rows)[0]
    assert c["status"] == cc.NO_PLAN and c["clipped"]
    assert c["add_n"] == 12 and c["suggest_s"] == 30.0
    assert "no short subs: add 12 x 30 s" in c["message"]
    assert "enable the goal's HDR set" in c["message"]


def test_coverage_too_few_and_covered(tmp_path):
    from photonscript.scheduler import core_check as cc
    cfg = _cfg(tmp_path)
    longs = [_row(300, 900) for _ in range(5)]
    shorts = [_row(30, 0, p999=20000) for _ in range(4)] + [_row(30, 0, verdict="pending")]
    c = cc.coverage(cfg, longs + shorts, _proj(30, 12))[0]
    assert c["status"] == cc.TOO_FEW and c["short_have"] == 4 and c["add_n"] == 8
    assert "4 of 12 short subs in the Library (+1 pending review): add 8 x 30 s" in c["message"]
    shorts = [_row(30, 0, p999=20000) for _ in range(12)]
    c = cc.coverage(cfg, longs + shorts, _proj(30, 12))[0]
    assert c["status"] == cc.COVERED
    # measured unclipped 30 s at p99.9 20000: 30 x (52000 - 256) / 19744 = 78 s -> 60 s
    assert c["suggest_s"] == 60.0


def test_coverage_short_clips_and_not_clipped(tmp_path):
    from photonscript.scheduler import core_check as cc
    cfg = _cfg(tmp_path)
    rows = [_row(300, 900) for _ in range(3)] + [_row(30, 300) for _ in range(12)]
    c = cc.coverage(cfg, rows, _proj(30, 12))[0]
    assert c["status"] == cc.SHORT_CLIPS and c["suggest_s"] == 10.0
    ok = cc.coverage(cfg, [_row(300, 3) for _ in range(4)])[0]
    assert ok["status"] == cc.OK and not ok["clipped"]
    # graded before the core check
    old = [dict(_row(300, 0), core_sat_px=None) for _ in range(3)]
    assert cc.coverage(cfg, old)[0]["status"] == cc.UNMEASURED


def test_norm_reads_records_and_index_rows():
    from photonscript.scheduler import core_check as cc
    rec = {"target": "M31", "rig": "rc16", "filter": "L", "exp_s": 300,
           "passed_qa": True, "reviewed": True, "core_sat_px": 40,
           "core_peak_adu": 65535, "core_p999": 65535, "sat_adu": 65000, "offset": 256}
    n = cc.norm(rec)
    assert n["verdict"] == "approved" and n["core_sat_px"] == 40 and n["sat_adu"] == 65000
    row = {"date": NIGHT, "target": "Andromeda Galaxy", "rig": "rc16", "filter": "L",
           "exp_s": 300, "verdict": "pending",
           "metrics": {"core_sat_px": 40, "core_p999": 65535, "core_sat_adu": 65000}}
    assert cc.norm(row)["core_sat_px"] == 40 and cc.norm(row)["verdict"] == "pending"


def test_morning_line_and_sub_doctor_notes(tmp_path):
    from photonscript.scheduler import core_check as cc
    cfg = _cfg(tmp_path)
    subs = [{"target": "M31", "rig": "rc16", "filter": "L", "exp_s": 300,
             "passed_qa": True, "reviewed": True, "core_sat_px": 900,
             "core_peak_adu": 65535, "core_p999": 65535, "sat_adu": 65000}
            for _ in range(4)]
    line = cc.morning_line(cfg, NIGHT, subs, [])
    assert line.startswith("Core clipped: ")
    assert "add 12 x 30 s" in line
    # healthy core: nothing to say
    for s in subs:
        s["core_sat_px"] = 0
    assert cc.morning_line(cfg, NIGHT, subs, []) is None
    from photonscript.scheduler.sub_doctor import format_report
    txt = format_report(cfg, {"night": NIGHT, "window": 3, "rigs": {},
                              "core": ["M31 L 300 s: core clipped ... add 12 x 30 s"]})
    assert "Core clipped, too few short subs: M31 L 300 s" in txt


def test_morning_report_card_has_core_line(tmp_path):
    from photonscript.scheduler import morning_report as mr
    from photonscript.scheduler.runs import runs_dir
    cfg = _cfg(tmp_path)
    d = runs_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{NIGHT}_subs.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({"file": f"LIGHT/m31_{i}.fits", "target": "M31",
                                 "rig": "rc16", "filter": "L", "exp_s": 300,
                                 "passed_qa": True, "reviewed": True,
                                 "core_sat_px": 800, "core_peak_adu": 65535,
                                 "core_p999": 65535, "sat_adu": 65000}) + "\n")
    card = mr.report_card(cfg, NIGHT, projects=[], calibration=False,
                          completeness=False)
    assert card["core"] and "add 12 x 30 s" in card["core"]
    assert card["core"] in card["lines"]


# ------------------------------------------------------------ master check

def _write_xisf(path: Path, img: np.ndarray):
    """A minimal monolithic XISF: Float32, planar, uncompressed."""
    h, w = img.shape
    data = img.astype("<f4").tobytes()
    pos = 4096
    xml = (f'<?xml version="1.0" encoding="UTF-8"?><xisf version="1.0" '
           f'xmlns="http://www.pixinsight.com/xisf"><Image geometry="{w}:{h}:1" '
           f'sampleFormat="Float32" bounds="0:1" colorSpace="Gray" '
           f'location="attachment:{pos}:{len(data)}"/></xisf>').encode("utf-8")
    head = b"XISF0100" + struct.pack("<I", len(xml)) + b"\0\0\0\0" + xml
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(head + b"\0" * (pos - len(head)) + data)
    return path


def _master(clipped: bool):
    yy, xx = np.mgrid[0:SIZE, 0:SIZE]
    r2 = (xx - 310) ** 2 + (yy - 290) ** 2
    img = 0.02 + 0.9 * np.exp(-r2 / (2 * 30.0 ** 2)) + 0.1 * np.exp(-r2 / (2 * 150.0 ** 2))
    if clipped:
        img = np.minimum(img, 0.62)
    rng = np.random.default_rng(2)
    return (img + rng.normal(0, 1e-4, img.shape)).astype(np.float32)


def test_read_xisf_roundtrip(tmp_path):
    from photonscript.integration.core_master import read_xisf
    img = _master(False)
    a = read_xisf(_write_xisf(tmp_path / "m.xisf", img))
    assert a.shape == (1, SIZE, SIZE)
    assert np.allclose(np.asarray(a[0]), img)


def test_master_check_plateau(tmp_path):
    from photonscript.integration.core_master import check_master
    r = check_master(_write_xisf(tmp_path / "master_L.xisf", _master(True)), 60.0, 25)
    assert r["still_clipped"] and r["clipped_px"] > 25
    assert r["line"] == f"core clipped in master_L: {r['clipped_px']} px (over 25)"
    r = check_master(_write_xisf(tmp_path / "master_L_hdr.xisf", _master(False)), 60.0, 25)
    assert not r["still_clipped"]
    assert r["line"] == f"core not clipped in master_L_hdr: {r['clipped_px']} px (limit 25)"
    bad = check_master(tmp_path / "missing.xisf", 60.0)
    assert "not checked" in bad["line"]


def test_master_check_run_prefers_hdr_and_logs(tmp_path):
    from photonscript.integration import core_master as cm
    out = tmp_path / "out"
    _write_xisf(out / "master" / "master_L.xisf", _master(True))
    _write_xisf(out / "master" / "master_L_30s.xisf", _master(False))
    _write_xisf(out / "master" / "master_L_hdr.xisf", _master(False))
    _write_xisf(out / "master" / "master_R.xisf", _master(True))
    stacks = [{"name": "L", "filter": "L", "groups": [{"exp": 300}]},
              {"name": "L_30s", "filter": "L", "groups": [{"exp": 30}]},
              {"name": "R", "filter": "R", "groups": [{"exp": 300}]}]
    (out / "pipeline.log").write_text("EXIT OK\n", encoding="ascii")
    lines = []
    res = cm.check_run(out, stacks, ["L_hdr"], r_px=60.0, warn_px=25, echo=lines.append)
    assert [r["master"] for r in res] == ["master_L_hdr", "master_R"]
    assert not res[0]["still_clipped"] and res[1]["still_clipped"]
    log = (out / "pipeline.log").read_text(encoding="ascii")
    assert "core not clipped in master_L_hdr: " in log and "core clipped in master_R: " in log
    # no HDR made: the long master
    res = cm.check_run(out, stacks, [], r_px=60.0, echo=lambda s: None)
    assert res[0]["master"] == "master_L" and res[0]["still_clipped"]
    # the ledger block
    from photonscript.integration import ledger
    assert callable(ledger.build)


# ------------------------------------------------------------ target page, CLI

def test_target_detail_core_block(tmp_path):
    from photonscript.scheduler import sub_index
    from photonscript.scheduler.runs import runs_dir
    cfg = _cfg(tmp_path)
    d = runs_dir(cfg)
    d.mkdir(parents=True, exist_ok=True)
    with (d / f"{NIGHT}_subs.jsonl").open("w", encoding="utf-8") as fh:
        for i in range(3):
            fh.write(json.dumps({"file": f"LIGHT/m31_{i}.fits", "target": "M31",
                                 "rig": "rc16", "filter": "L", "exp_s": 300,
                                 "passed_qa": True, "reviewed": True,
                                 "core_sat_px": 800, "core_peak_adu": 65535,
                                 "core_p999": 65535, "sat_adu": 65000,
                                 "offset": 256}) + "\n")
    det = sub_index.target_detail(cfg, "M31", [])
    assert det["core"]["lines"] and "core clipped on 3 of 3 subs" in det["core"]["lines"][0]
    tpl = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
           / "templates" / "target.html").read_text(encoding="utf-8")
    assert 'id="tCore"' in tpl and "renderCore()" in tpl


def test_cli_core_check_measure(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    lib = tmp_path / "Library" / "Andromeda Galaxy"
    for i in range(3):
        _write(lib / "L" / f"m31_L_300s_{i}.fits", _frame(seed=i))
    for i in range(2):
        _write(lib / "L" / f"m31_L_30s_{i}.fits", _frame(core_amp=9000.0, seed=10 + i),
               exp=30.0)
    monkeypatch.setattr(cli, "_config_for_repo",
                        lambda repo: _cfg(tmp_path))
    r = CliRunner().invoke(cli.app, ["core-check", "--target", "M31",
                                     "--measure", str(lib)], env={"COLUMNS": "400"})
    assert r.exit_code == 0, r.output
    assert "Core check: M31" in r.output
    assert "too_few" in r.output and "2 of 12 short subs" in r.output
    # shot at 30 s: add more of those; the measured better length rides along
    assert "add 10 x 30 s" in r.output and "measured: 30 s puts the core" in r.output
    r = CliRunner().invoke(cli.app, ["core-check", "--target", "M31",
                                     "--measure", str(lib), "--json"])
    body = json.loads(r.output)
    g = {(x["exp_s"]): x for x in body["groups"]}
    assert g[300.0]["clipped"] == 3 and g[30.0]["clipped"] == 0
    assert math.isfinite(body["coverage"][0]["suggest_s"])


def test_live_grader_records_core(tmp_path):
    import asyncio
    from photonscript.scheduler.runs import _load_subs
    from tests.test_scheduler.test_ps21_grading import _agent
    pytest.importorskip("sep")
    cfg = _cfg(tmp_path)
    f = _write(tmp_path / "fits" / NIGHT / "LIGHT" / "m31_L_300s_0001.fits", _frame())
    a = _agent(cfg)
    asyncio.run(a._process_new_image(f))
    (rec,) = _load_subs(cfg, NIGHT)
    assert rec["target"] == "M31" and rec["core_sat_px"] > 100
    assert rec["core_src"] == "header" and rec["core_p999"] >= 65000
    rows = {r[0]: r for r in rec["scorecard"]["rows"]}
    assert rows["core"][1] == rec["core_sat_px"] and rows["core"][3] == q.PASS
    # a rescore of the stored record gives the same row
    k = q.group_key(rec)
    again = q.evaluate(q.metrics_from_record(rec), q.context(cfg, k[0], k[1], k[2]))
    assert next(c for c in again.checks if c.id == "core").row() == rows["core"]


def test_pipeline_checks_the_hdr_master_into_ledger_and_log(mono_lib, tmp_path,  # noqa: F811
                                                           monkeypatch):
    from photonscript.integration import pipeline as pl
    from tests.test_integration.test_hdr_combine import _fake_runner
    from tests.test_integration.test_ps177_178_exposure_groups import _opts
    lib = mono_lib
    base = _fake_runner([])

    def fake(script, log_path, **kw):
        r = base(script, log_path, **kw)
        m = Path(log_path).parent / "master"
        for f in m.glob("master_*.xisf"):          # real XISF: long clipped, HDR not
            _write_xisf(f, _master(clipped=not f.stem.endswith("_hdr")))
        return r
    monkeypatch.setattr(pl.runner, "run_script", fake)
    out = tmp_path / "Staging" / "core"
    msgs = []
    res = pl.run(_opts(lib, tmp_path, out=out, allow_uncalibrated=True, run_pixinsight=True),
                 echo=msgs.append)
    cc = {c["master"]: c for c in res["core_check"]}
    assert "master_L_hdr" in cc and not cc["master_L_hdr"]["still_clipped"]
    led = json.loads(Path(res["ledger"]).read_text())
    assert any(c["master"] == "master_L_hdr" for c in led["machine"]["core_check"])
    log = (out / "out" / "pipeline.log").read_text(encoding="ascii")
    assert "core not clipped in master_L_hdr: " in log
    assert any("core not clipped in master_L_hdr" in m for m in msgs)


def test_warn_px_scales_with_the_rig_pixel_area(tmp_path):
    cfg = _cfg(tmp_path, pixel_scale_arcsec=0.236, piggyback_pixel_scale_arcsec=1.29)
    assert cs.warn_px(cfg, "rc16") == 25
    assert cs.warn_px(cfg, "piggyback") == cs.MIN_WARN_PX       # 25 x 0.033 -> 4
    assert q.thresholds(cfg, "piggyback")["core_sat_warn_px"] == cs.MIN_WARN_PX
    from photonscript.scheduler import core_check as cc
    rows = [dict(_row(300, 10), rig="piggyback", filter="OSC") for _ in range(4)] + \
        [dict(_row(120, 0, p999=30000), rig="piggyback", filter="OSC") for _ in range(20)]
    c = cc.coverage(cfg, rows)[0]
    assert c["clipped"] and c["status"] == cc.COVERED
    # 2026-10-14: integrate's OSC core combine fills it (integrate_osc_hdr_core)
    assert "master_OSC_hdr" in c["message"]
