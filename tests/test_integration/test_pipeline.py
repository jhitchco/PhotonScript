"""PS-22: the integrate pipeline end to end on tiny FITS files (no
PixInsight, no star QA: those have their own tests)."""
import json
from pathlib import Path

import numpy as np
import pytest

from photonscript.integration import pipeline as pl
from photonscript.integration import pjsr
from photonscript.integration.frames import Frame


def _fits(path, kind, exp, date_loc, date_obs, temp=0.0, foc=21.0):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = kind
    h["EXPTIME"] = exp
    h["GAIN"] = 100
    h["OFFSET"] = 256
    h["SET-TEMP"] = temp
    h["READOUTM"] = "Low Conversion Gain"
    h["INSTRUME"] = "AP26CC"
    h["BAYERPAT"] = "RGGB"
    h["XBINNING"] = 1
    h["FOCALLEN"] = 600.0
    h["FOCRATIO"] = 5.6
    h["XPIXSZ"] = 3.76
    h["FOCTEMP"] = foc
    h["DATE-LOC"] = date_loc
    h["DATE-OBS"] = date_obs
    h["SWCREATE"] = "N.I.N.A. 3.2.0.9001 (x64)"
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((8, 8), np.uint16), header=h).writeto(path)


@pytest.fixture
def lib(tmp_path):
    L = tmp_path / "Library"
    for i in range(3):
        _fits(L / "Andromeda Galaxy" / "OSC" / f"2026-09-21_03-0{i}-00__0.00_120.00s_000{i}.fits",
              "LIGHT", 120.0, f"2026-09-21T03:0{i}:00", f"2026-09-21T09:0{i}:00")
    for i in range(2):
        _fits(L / "M 31" / "OSC" / f"2026-10-03_20-0{i}-00__0.00_400.00s_000{i}.fits",
              "LIGHT", 400.0, f"2026-10-03T20:0{i}:00", f"2026-10-04T02:0{i}:00", foc=23.0)
    _fits(L / "_rejected" / "M 31" / "OSC" / "rej.fits", "LIGHT", 400.0, "2026-10-03T21:00:00", "")
    for i in range(12):
        _fits(L / "piggyback" / "Calibration" / "BIAS" / "2026-09-12" / f"b{i:02d}.fits",
              "BIAS", 0.0, "2026-09-12T21:00:00", "")
        _fits(L / "piggyback" / "Calibration" / "DARK" / "2026-09-12" / f"d{i:02d}.fits",
              "DARK", 120.0, "2026-09-12T19:00:00", "")
    for i in range(6):
        _fits(L / "piggyback" / "Calibration" / "FLAT" / "2026-09-21" / f"f{i}.fits",
              "FLAT", 0.14, "2026-09-21T19:15:00", "", temp=10.0)
    return L


def _opts(lib, tmp_path, **kw):
    o = pl.Options(target="M31", rig="piggyback", library=lib, staging_root=tmp_path / "Staging",
                   qa="off", run_pixinsight=False, pixinsight=str(tmp_path / "PI" / "bin" / "PixInsight.exe"),
                   default_readout="LCG", site={"name": "AARO", "lat": 31.907, "lon": -109.021,
                                                "elev": 1300, "bortle": 2})
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _snapshot(root: Path):
    return {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in root.rglob("*")}


def test_full_run_without_pixinsight(lib, tmp_path):
    before = _snapshot(lib)
    out = tmp_path / "Staging" / "run1"
    res = pl.run(_opts(lib, tmp_path, out=out), echo=lambda s: None)
    assert _snapshot(lib) == before                     # Library untouched
    assert res["lights_selected"] == 5 and res["lights_staged"] == 5
    assert len(list((out / "LIGHTS" / "OSC" / "120s").glob("*.fits"))) == 3
    assert len(list((out / "LIGHTS" / "OSC" / "400s").glob("*.fits"))) == 2
    assert len(list((out / "DARKS" / "120s").glob("*.fits"))) == 12
    assert len(list((out / "BIAS").glob("*.fits"))) == 12
    assert not (out / "FLATS").exists()                 # warm flats excluded
    man = json.loads((out / "manifest.json").read_text())
    assert man["counts"] == {"lights_selected": 5, "lights_staged": 5, "lights_rejected_by_qa": 0,
                             "bias": 12, "darks": 12, "flats": 0}
    dk = {d["light_exp"]: d for d in man["calibration"]["darks"]}
    assert dk[400.0]["scaled"] and dk[400.0]["dark_exp"] == 120.0 and not dk[120.0]["scaled"]
    js = (out / "integrate_run.js").read_text()
    assert pjsr.check(js) == []
    cfg = json.loads(js.split("var CONFIG = ", 1)[1].split(";\n", 1)[0])
    g = {x["name"]: x for x in cfg["stacks"][0]["groups"]}
    assert g["400s"]["optimize"] and g["400s"]["dark"] == 120.0 and not g["120s"]["optimize"]
    assert cfg["cfa"] == "RGGB" and cfg["bias"] == {"dir": "BIAS"}
    assert pjsr.check((out / "finish_run.js").read_text()) == []
    csvs = list((out / "astrobin").glob("*_astrobin_acquisition.csv"))
    lines = csvs[0].read_text().splitlines()
    assert lines[1] == "2026-09-21,3,120,1,100,0,5.6,12,0,0,12,2,21"
    assert lines[2] == "2026-10-03,2,400,1,100,0,5.6,12,0,0,12,2,23"
    md = next((out / "astrobin").glob("*_packet.md")).read_text()
    md.encode("ascii")
    assert "PLANNED only" in md and "uncooled flats" in md
    timing = (out / "out" / "timing.csv").read_text().splitlines()
    assert timing[0] == "stage,start_utc,end_utc,minutes,status"
    assert any(l.startswith("stage (copy") for l in timing)


def test_dry_run_writes_nothing(lib, tmp_path):
    out = tmp_path / "Staging" / "dry"
    res = pl.run(_opts(lib, tmp_path, out=out, dry_run=True), echo=lambda s: None)
    assert res["dry_run"] and not out.exists()


def test_refuses_a_non_empty_run_folder(lib, tmp_path):
    out = tmp_path / "Staging" / "old"
    out.mkdir(parents=True)
    (out / "x.txt").write_text("earlier run")
    with pytest.raises(pl.PipelineError, match="NEW folder"):
        pl.run(_opts(lib, tmp_path, out=out), echo=lambda s: None)


def test_refuses_a_run_folder_inside_the_library(lib, tmp_path):
    with pytest.raises(pl.PipelineError, match="outside the Library"):
        pl.run(_opts(lib, tmp_path, out=lib / "stage"), echo=lambda s: None)


def test_copy_into_refuses_library(lib, tmp_path):
    src = next(lib.rglob("*.fits"))
    with pytest.raises(pl.PipelineError):
        pl.copy_into(src, lib / "M 31" / "x", lib)
    d = pl.copy_into(src, tmp_path / "S", lib)
    assert d.exists() and src.exists()


def test_no_lights_is_an_error(lib, tmp_path):
    with pytest.raises(pl.PipelineError, match="no approved"):
        pl.run(_opts(lib, tmp_path, target="Crescent Nebula", out=tmp_path / "S" / "c"),
               echo=lambda s: None)


def test_dropped_from_log():
    txt = ("2026  DROPPED at registration: 2026-09-21_03-00-00__0.00_120.00s_0000_c_cc_d.xisf\n"
           "  DROPPED at local normalization (no map): x_c_cc_d_r.xisf\nother\n")
    assert pl.dropped_from_log(txt) == {"2026-09-21_03-00-00__0.00_120.00s_0000", "x"}


def test_limit_per_group_even_spacing():
    fs = [Frame(path=Path(f"a{i:02d}.fits"), exp=120.0, filter="OSC", date_obs=f"T{i:02d}")
          for i in range(10)]
    fs += [Frame(path=Path("b.fits"), exp=400.0, filter="OSC", date_obs="T99")]
    got = pl.limit_per_group(fs, 3)
    assert [f.name for f in got] == ["a00.fits", "a03.fits", "a06.fits", "b.fits"]


def test_reference_per_filter_picks_sharp_deep_sub():
    rows = [{"file": "a.fits", "filter": "Ha", "stars": 900, "hfd_px": 5.0},
            {"file": "b.fits", "filter": "Ha", "stars": 800, "hfd_px": 3.0},
            {"file": "c.fits", "filter": "Ha", "stars": 700, "hfd_px": 3.1},
            {"file": "d.fits", "filter": "OIII", "stars": 100, "hfd_px": 3.0}]
    ref = pl.reference_per_filter(rows, {"a.fits", "b.fits", "c.fits", "d.fits"})
    assert ref == {"Ha": "b", "OIII": "d"}
