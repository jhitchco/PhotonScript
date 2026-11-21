"""PS-265: Seestar S50 Pro support in `photonscript integrate`: plain folder
input (not the Library), GRBG OSC lights only, mixed sub lengths in one
stack, optional ready master darks, Generalized ESD, drizzle 2x, pruning.
No PixInsight here: the generated script is checked (and parsed by node
when it is on PATH)."""
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from photonscript.integration import pipeline as pl
from photonscript.integration import pjsr
from photonscript.integration.frames import read_frame
from photonscript.integration.select import select_folders

NODE = shutil.which("node")


def _seestar(path: Path, exp=10.0, filt="LP", when="2026-10-08T03:01:38.786484", temp=22.06,
             obj="NGC 281"):
    """A Seestar-like sub: CREATOR (no INSTRUME / SWCREATE), BAYERPAT GRBG,
    FOCUSPOS, SITELAT / SITELONG, RA / DEC in degrees, no FOCRATIO."""
    from astropy.io import fits
    h = fits.Header()
    h["CREATOR"] = "ZWO Seestar S50 Pro"
    h["BIAS"] = 2064
    h["FOCALLEN"] = 260.0
    h["EQMODE"] = 1
    h["XBINNING"] = 1
    h["XPIXSZ"] = 2.9
    h["IMAGETYP"] = "Light"
    h["EXPTIME"] = exp
    h["CCD-TEMP"] = temp
    h["RA"] = 13.745835
    h["DEC"] = 56.723611
    h["SITELONG"] = -71.468
    h["SITELAT"] = 43.0138
    h["DATE-OBS"] = when
    h["FILTER"] = filt
    h["BAYERPAT"] = "GRBG"
    h["GAIN"] = 200
    h["FOCUSPOS"] = 950
    h["TELESCOP"] = "S50 Pro_aef8af7f"
    h["OBJECT"] = obj
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((8, 8), np.uint16), header=h).writeto(path)


@pytest.fixture
def works(tmp_path):
    """MyWorks/<target>_sub folders with .jpg and _thn.jpg beside each sub."""
    w = tmp_path / "MyWorks"
    lp, ir = w / "NGC 281_sub", w / "NGC 281W_sub"
    for i in range(4):
        n = f"Light_NGC 281_10.0s_LP_20261007-23015{i}"
        _seestar(lp / f"{n}.fit", when=f"2026-10-08T03:01:5{i}")
        (lp / f"{n}.jpg").write_bytes(b"jpg")
        (lp / f"{n}_thn.jpg").write_bytes(b"jpg")
    for i in range(3):
        _seestar(lp / f"Light_NGC 281_60.0s_LP_20261121-03{i}000.fit", exp=60.0,
                 when=f"2026-11-21T03:{i}0:00", temp=9.4)
    for i in range(3):
        _seestar(ir / f"Light_NGC 281W_10.0s_IRCUT_20261119-2141{i}0.fit", filt="IRCUT",
                 when=f"2026-11-20T03:41:{i}0", temp=10.8, obj="NGC 281W")
    (w / "darklibrary").mkdir()
    dark = w / "darklibrary" / "MasterDark_5_10.0s_11.4C.fit"
    _seestar(dark)
    return w


def _opts(tmp_path, dirs, **kw):
    o = pl.Options(target="NGC 281", rig="seestar", library=tmp_path / "Library",
                   staging_root=tmp_path / "Staging", qa="off", run_pixinsight=False,
                   pixinsight=str(tmp_path / "PI" / "bin" / "PixInsight.exe"),
                   source_dirs=list(dirs), rig_label="ZWO Seestar S50 Pro", osc_hdr_set_max_s=0.0,
                   osc_hdr_core=False, drizzle=2, rejection="esd", prune=True, focratio=5.0,
                   site={"name": "AARO", "lat": 31.907, "lon": -109.021, "elev": 1300, "bortle": 2})
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _snapshot(root: Path):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def _cfg(js: str) -> dict:
    return json.loads(js.split("var CONFIG = ", 1)[1].split(";\n", 1)[0])


def test_seestar_header_fields(works):
    f = read_frame(works / "NGC 281_sub" / "Light_NGC 281_10.0s_LP_20261007-230150.fit")
    assert f.instrument == "ZWO Seestar S50 Pro" and f.software == "ZWO Seestar S50 Pro"
    assert f.is_osc and f.bayer == "GRBG" and f.filter == "LP"
    assert f.exp == 10.0 and f.gain == 200 and f.ccd_temp == pytest.approx(22.06)
    assert f.site_lat == pytest.approx(43.0138) and f.focpos == 950


def test_select_folders_reads_only_the_light_pattern(works):
    sel = select_folders([works / "NGC 281_sub", works / "NGC 281W_sub"], "NGC 281")
    assert len(sel.lights) == 10                     # .jpg / _thn.jpg ignored
    assert {f.filter for f in sel.lights} == {"LP", "IRCUT"}
    assert sorted({f.exp for f in sel.lights}) == [10.0, 60.0]
    assert sel.rig == "folder" and len(sel.folders) == 2
    only_ir = select_folders([works / "NGC 281_sub", works / "NGC 281W_sub"], "NGC 281", filters=["ircut"])
    assert len(only_ir.lights) == 3
    late = select_folders([works / "NGC 281_sub"], "NGC 281", since="2026-11-01")
    assert len(late.lights) == 3 and all(f.exp == 60.0 for f in late.lights)


def test_select_folders_missing_folder_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        select_folders([tmp_path / "nope"], "X")


def test_names_and_tags():
    assert pl.staged_name("Light_NGC 281_10.0s_LP_x.fit") == "Light_NGC_281_10.0s_LP_x.fit"
    assert pl.exposure_tag(10.0, ["Light_M_42_10.0s_LP_x.fit"]) == "_10.0s_"
    assert pl.exposure_tag(60.0, ["Light_M_42_60.0s_LP_x.fit"]) == "_60.0s_"
    assert pl.exposure_tag(120.0, ["2026-09-21__0.00_120.00s_0001.fits"]) == "_120.00s_"
    assert pl.folder_site([]) == {}


def test_folder_run_without_pixinsight(works, tmp_path):
    before = _snapshot(works)
    out = tmp_path / "Staging" / "seestar1"
    dark = works / "darklibrary" / "MasterDark_5_10.0s_11.4C.fit"
    res = pl.run(_opts(tmp_path, [works / "NGC 281_sub", works / "NGC 281W_sub"], out=out,
                       dark_files={10.0: str(dark)}), echo=lambda s: None)
    assert _snapshot(works) == before                # source folders untouched
    assert res["lights_selected"] == 10 and res["lights_staged"] == 10
    lp10 = sorted(p.name for p in (out / "LIGHTS" / "LP" / "10s").iterdir())
    assert len(lp10) == 4 and all(" " not in n for n in lp10)     # spaces -> underscores
    assert len(list((out / "LIGHTS" / "LP" / "60s").iterdir())) == 3
    assert len(list((out / "LIGHTS" / "IRCUT" / "10s").iterdir())) == 3
    assert not (out / "BIAS").exists() and not (out / "DARKS").exists() and not (out / "FLATS").exists()
    js = (out / "integrate_run.js").read_text("ascii")
    assert pjsr.check(js) == []
    cfg = _cfg(js)
    assert cfg["cfa"] == "GRBG" and cfg["bias"] is None and cfg["darks"] == [] and cfg["flats"] == []
    assert cfg["drizzle"] == 2 and cfg["rejection"] == "esd" and cfg["prune"] is True
    st = {s["name"]: s for s in cfg["stacks"]}
    assert set(st) == {"LP", "IRCUT"}
    # 10 s and 60 s in ONE LP stack (no Piggy HDR short set), PSF weights
    lp = st["LP"]
    assert [g["exp"] for g in lp["groups"]] == [10.0, 60.0] and lp["mixed_exposures"] is True
    assert "short_subset" not in lp and "exclude_groups" not in lp
    g10, g60 = lp["groups"]
    assert g10["tag"] == "_10.0s_" and g60["tag"] == "_60.0s_"
    assert g10["dark_file"].endswith("MasterDark_5_10.0s_11.4C.fit") and "dark_file" not in g60
    assert g10["dark"] is None
    led = json.loads((out / "ledger.json").read_text())
    assert led["rig"] == "seestar"
    pk = next((out / "astrobin").glob("*_astrobin_packet.md")).read_text("ascii")
    assert "Lights only (no bias or flats, master dark for 10 s subs)" in pk
    assert "VNG debayer (GRBG)" in pk and "Uncooled sensor" in pk
    assert "Drizzle 2x was asked for" not in pk             # PixInsight did not run
    assert "ZWO Seestar S50 Pro" in pk
    rows = next((out / "astrobin").glob("*_acquisition.csv")).read_text("ascii").splitlines()
    assert rows[0].startswith("date,number,duration")
    assert any(",200," in r and ",5," in r for r in rows[1:])      # gain 200, f/5
    assert res["calibration"]["refused_uncalibrated"] == []


def test_folder_run_never_writes_inside_an_input_folder(works, tmp_path):
    with pytest.raises(pl.PipelineError):
        pl.run(_opts(tmp_path, [works / "NGC 281_sub"], out=works / "NGC 281_sub" / "run"),
               echo=lambda s: None)
    with pytest.raises(pl.PipelineError):
        pl.copy_into(works / "NGC 281_sub" / "x.fit", works / "NGC 281_sub" / "sub", tmp_path / "Library",
                     protect=[works / "NGC 281_sub"])


def test_folder_run_skips_the_progression_sheet(works, tmp_path, monkeypatch):
    from photonscript.integration import progression
    called = []
    monkeypatch.setattr(progression, "after_run", lambda *a, **k: called.append(1))
    pl.run(_opts(tmp_path, [works / "NGC 281_sub"], out=tmp_path / "Staging" / "s2"), echo=lambda s: None)
    assert called == []


def test_drizzle_master_path(tmp_path):
    assert pl.drizzle_master_path(tmp_path, "LP", 2) == tmp_path / "master" / "drizzle" / "master_LP_drizzle2x.xisf"


def test_template_drizzle_esd_prune_lights_only():
    t = pjsr.template(pjsr.INTEGRATE_TEMPLATE)
    assert pjsr.check(t.replace("//__CONFIG__", "var CONFIG = {};")) == []
    # lights only: no ImageCalibration when nothing is there to calibrate
    assert "if (!bias && !dark && !flat) {" in t and "return lights.slice(0);" in t
    assert "if (g.dark_file && File.exists(g.dark_file)) dark = darkAsXisf(g.dark_file);" in t
    # drizzle: SA writes drizzle data (not for pass 3), II updates it, DI runs
    assert "SA.generateDrizzleData = true" in t and "noDrizzle: true" in t
    assert "II.generateDrizzleData = true" in t
    assert "new DrizzleIntegration" in t and "DI.integrationImageId" in t
    assert 'CONFIG.rejection === "esd"' in t and "Rejection_ESD" in t
    body = t[t.index("function drizzleStack("):t.index("// OSC core combine, 2026-10-14")]
    assert "catch (e)" in body and "stands" in body          # optional, guarded
    assert "if (CONFIG.prune)" in t


@pytest.mark.skipif(not NODE, reason="node not on PATH")
def test_rendered_folder_script_parses(works, tmp_path):
    out = tmp_path / "Staging" / "parse"
    pl.run(_opts(tmp_path, [works / "NGC 281_sub"], out=out), echo=lambda s: None)
    js = (out / "integrate_run.js").read_text("ascii")
    stub = tmp_path / "parse.js"
    stub.write_text(js.replace("#include <pjsr/DataType.jsh>", ""), encoding="ascii")
    r = subprocess.run([NODE, "--check", str(stub)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_seestar_ledger_stays_local(works, tmp_path):
    from photonscript.integration import finish_retry, report
    root = tmp_path / "Staging"
    res = pl.run(_opts(tmp_path, [works / "NGC 281_sub"], out=root / "seestar_led"), echo=lambda s: None)
    led = Path(res["ledger"])
    assert json.loads(led.read_text())["rig"] == "seestar"
    assert report.pending(root) == []                       # never queued for the scheduler
    posted = []
    r = report.post_ledger(led, "http://scheduler.invalid", post=lambda *a: posted.append(a) or (200, {"ok": 1}))
    assert r["ok"] is False and posted == []
    assert finish_retry.newest_runs(root) == []             # autointegrate never retries it


def test_unregistered_sub_kept_when_registration_not_required():
    from photonscript.integration import star_qa
    base = dict(stars=300, hfd_px=3.0, ecc=0.5, sky_adu=100.0, double_frac=0.0, filter="LP", exp=10.0,
                aligned=False, coverage=0.0, is_reference=False)
    rows = [dict(base, time=f"2026-10-08T03:0{i}:00") for i in range(6)]
    star_qa.classify([dict(r) for r in rows], star_qa.Thresholds())
    strict = [dict(r) for r in rows]
    star_qa.classify(strict, star_qa.Thresholds())
    assert all("not_registered" in r["reason"] for r in strict)
    loose = [dict(r) for r in rows]
    star_qa.classify(loose, star_qa.Thresholds(require_registration=False))
    assert all(r["action"] == "keep" for r in loose)
    loose[0]["ecc"] = 0.8
    star_qa.classify(loose, star_qa.Thresholds(require_registration=False))
    assert loose[0]["action"] == "reject" and "trailed" in loose[0]["reason"]


def _fake_run(tmp_path, drizzle=True):
    run = tmp_path / "Staging" / "NGC_281_seestar_20261121-1200"
    (run / "out" / "master" / "drizzle").mkdir(parents=True)
    for s in ("LP", "IRCUT"):
        (run / "out" / "master" / f"master_{s}.xisf").write_bytes(b"x")
        if drizzle:
            (run / "out" / "master" / "drizzle" / f"master_{s}_drizzle2x.xisf").write_bytes(b"x")
    (run / "ledger.json").write_text(json.dumps({"rig": "seestar", "campaign": "NGC 281"}))
    (run / "finish_run.js").write_text("var FOCAL_MM  = 260;\nvar PIXEL_UM  = 1.45;\n", encoding="ascii")
    return run


def test_osc_split_plan_prefers_the_drizzle_master(tmp_path):
    from photonscript.integration import osc_split as osp
    run = _fake_run(tmp_path)
    items = osp.plan(run, hoo="LP", rgb="IRCUT")
    assert [(i["kind"], i["drizzle"]) for i in items] == [("hoo", 2), ("rgb", 2)]
    assert items[0]["src"].endswith("drizzle/master_LP_drizzle2x.xisf")
    with pytest.raises(osp.SplitError):
        osp.plan(run)
    with pytest.raises(osp.SplitError):
        osp.plan(run, hoo="OIII")
    plain = _fake_run(tmp_path / "b", drizzle=False)
    assert osp.plan(plain, rgb="IRCUT")[0]["src"].endswith("master/master_IRCUT.xisf")


def test_osc_split_writes_a_new_folder_and_a_valid_script(tmp_path):
    from photonscript.integration import osc_split as osp
    run = _fake_run(tmp_path)
    before = _snapshot(run)
    res = osp.run(run, hoo="LP", rgb="IRCUT", run_pixinsight=False, echo=lambda s: None)
    assert _snapshot(run) == before                         # the run is only read
    split = Path(res["split_dir"])
    assert split.name == run.name + "_split" and split.parent == run.parent
    js = (split / "split_run.js").read_text("ascii")
    assert pjsr.check(js) == []
    assert '"$T[0]"' in js and '"($T[1] + $T[2]) / 2"' in js
    man = json.loads((split / "manifest.json").read_text())
    assert man["rig"] == "seestar" and man["target"] == "NGC 281" and man["pixel_um"] == 1.45
    assert "--rig seestar" in res["finish_line"] and "--pixel-um 1.45" in res["finish_line"]
    with pytest.raises(osp.SplitError):                     # never over a non-empty folder
        osp.run(run, hoo="LP", out=split, run_pixinsight=False, echo=lambda s: None)
    with pytest.raises(osp.SplitError):                     # never inside the run
        osp.run(run, hoo="LP", out=run / "out" / "x", run_pixinsight=False, echo=lambda s: None)


def test_split_folder_is_a_seestar_mono_run(tmp_path):
    from photonscript.integration import finish as fn
    from photonscript.integration import mono_color as mc
    split = tmp_path / "split"
    (split / "out" / "master").mkdir(parents=True)
    for f in ("Ha", "OIII", "R", "G", "B"):
        (split / "out" / "master" / f"master_{f}.xisf").write_bytes(b"x")
    (split / "manifest.json").write_text(json.dumps({"rig": "seestar", "target": "NGC 281"}))
    items, _ = mc.plan(mc.run_masters(split), "NGC_281")
    kinds = {it["kind"]: it for it in items}
    assert set(kinds) == {"rgb", "hoo"} and kinds["hoo"]["stars_from"] == "rgb"
    assert fn.RIG_OPTICS["seestar"] == (260.0, 2.9)


def test_prune_keeps_the_shared_registration_reference():
    t = pjsr.template(pjsr.INTEGRATE_TEMPLATE)
    body = t[t.index("if (CONFIG.prune) {"):t.index('log("[" + S + "] funnel "')]
    assert "keepRef" in body and "continue;" in body and "reg.file" in body


def test_local_dates_from_site_longitude():
    from photonscript.integration.frames import Frame
    assert pl.zone_for_lon(-71.468) == "America/New_York" and pl.zone_for_lon(-109.0) == "America/Denver"
    f = Frame(path=Path("x.fit"), date_obs="2026-10-08T03:01:38.786484")
    g = Frame(path=Path("y.fit"), date_obs="2026-11-21T00:29:00", date_loc="2026-11-20T19:29:00")
    assert pl.localize_dates([f, g], "America/New_York") == 1
    assert f.date_loc == "2026-10-07T23:01:38" and f.night == "2026-10-07"
    assert g.date_loc == "2026-11-20T19:29:00"


def test_folder_packet_names_the_seestar_site_not_aaro(works, tmp_path):
    out = tmp_path / "Staging" / "site"
    pl.run(_opts(tmp_path, [works / "NGC 281_sub"], out=out), echo=lambda s: None)
    pk = next((out / "astrobin").glob("*_astrobin_packet.md")).read_text("ascii")
    assert "Seestar site" in pk and "43.014 N" in pk and "71.468 W" in pk
    rows = next((out / "astrobin").glob("*_acquisition.csv")).read_text("ascii").splitlines()[1:]
    assert all(r.split(",")[11] == "" for r in rows)          # bortle unknown, never AARO's 2
    assert rows[0].startswith("2026-10-07,")                   # local evening date, not UTC


def test_fits_master_dark_is_saved_as_xisf_first():
    t = pjsr.template(pjsr.INTEGRATE_TEMPLATE)
    assert "dark = darkAsXisf(g.dark_file);" in t
    body = t[t.index("function darkAsXisf("):t.index("function calibrateGroup(")]
    assert '".xisf"' in body and "/master/darks" in body and "forceClose" in body
