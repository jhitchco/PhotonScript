"""`photonscript integrate` never stacks a light whose FOCPOS is 0 (the
focuser was disconnected or frozen, 2026-10-08 RC16 EAF): left out in every
--qa mode, counted as "focuser_zero N" in the group summary."""
import numpy as np
import pytest

from photonscript.integration import pipeline as pl
from photonscript.integration import star_qa as q
from photonscript.integration.frames import Frame, from_header


def _fits(path, kind, exp, focpos=None, filt="L", minute=0):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = kind
    h["EXPTIME"] = exp
    h["GAIN"] = 200
    h["OFFSET"] = 256
    h["SET-TEMP"] = 0.0
    h["READOUTM"] = "High Conversion Gain"
    h["INSTRUME"] = "AP26MC"
    h["XBINNING"] = 1
    if filt:
        h["FILTER"] = filt
    if focpos is not None:
        h["FOCPOS"] = focpos
    h["DATE-LOC"] = f"2026-10-08T21:{minute:02d}:00"
    h["DATE-OBS"] = f"2026-10-09T03:{minute:02d}:00"
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((8, 8), np.uint16), header=h).writeto(path)


@pytest.fixture
def lib(tmp_path):
    L = tmp_path / "Library"
    for i, fp in enumerate((5845, 5850, None, 0, 0)):
        _fits(L / "M 31" / "L" / f"2026-10-08_21-{i:02d}-00__L_30.00s_{i:04d}.fits", "LIGHT", 30.0,
              focpos=fp, minute=i)
    for i in range(12):   # darks at FOCPOS 0 are fine (the 2026-10-08 quarantine darks)
        _fits(L / "Calibration" / "DARK" / "2026-10-08" / f"d30_{i:02d}.fits", "DARK", 30.0,
              focpos=0, filt="")
    return L


def _opts(lib, tmp_path, **kw):
    o = pl.Options(target="M31", rig="rc16", library=lib, staging_root=tmp_path / "Staging",
                   qa="off", run_pixinsight=False, flats=False, default_readout="HCG",
                   pixinsight=str(tmp_path / "PI" / "bin" / "PixInsight.exe"), dry_run=True)
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def _fake_run_qa(frames, *, workers=None, thr=None, echo=print):
    rows = []
    for f in frames:
        r = {"file": f.name, "night": f.night, "filter": f.filter, "exp": round(f.exp, 2),
             "time": f.date_obs, "xbin": 1, "readout": f.readout or "", "action": "keep",
             "reason": "stars_ok", "group_reference": frames[0].name, "stars": 100, "hfd_px": 3.0}
        r["group"] = q.group_label(q.group_key(r))
        rows.append(r)
    return rows, 0


def test_frame_reads_focpos():
    f = from_header("x.fits", {"IMAGETYP": "LIGHT", "FOCPOS": 0})
    assert f.focpos == 0 and q.is_focuser_zero(f)
    assert from_header("x.fits", {"FOCPOS": 5845}).focpos == 5845
    nofp = from_header("x.fits", {})
    assert nofp.focpos is None and not q.is_focuser_zero(nofp)


def test_focuser_zero_rows_count_in_the_group_summary():
    frames = [Frame(path=__import__("pathlib").Path(f"z{i}.fits"), filter="L", exp=30.0,
                    focpos=0) for i in range(2)]
    rows = [{"file": "k.fits", "filter": "L", "exp": 30.0, "xbin": 1, "readout": "",
             "action": "reject", "reason": "trailed(ecc 0.80)", "group": "L 30 s"}]
    rows += q.focuser_zero_rows(frames)
    g = q.group_summary(rows)
    assert len(g) == 1 and g[0]["subs"] == 3 and g[0]["reject"] == 3
    assert g[0]["reasons"] == {"trailed": 1, "focuser_zero": 2}


def test_report_mode_flags_and_still_leaves_them_out(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(q, "run_qa", _fake_run_qa)
    msgs = []
    res = pl.run(_opts(lib, tmp_path, qa="report"), echo=msgs.append)
    assert res["lights_staged"] == 3
    assert res["focuser_zero"]["n"] == 2 and res["focuser_zero"]["excluded"]
    assert res["qa"]["rejected"] == 2 and dict(res["qa"]["reasons"]) == {"focuser_zero": 2}
    assert any("left out 2 light(s) with FOCPOS 0" in m for m in msgs)
    assert any(m.strip().startswith("group L 30 s") and "focuser_zero 2" in m for m in msgs)


def test_apply_and_off_modes_leave_them_out(lib, tmp_path, monkeypatch):
    monkeypatch.setattr(q, "run_qa", _fake_run_qa)
    assert pl.run(_opts(lib, tmp_path, qa="apply"), echo=lambda s: None)["lights_staged"] == 3
    assert pl.run(_opts(lib, tmp_path, qa="off"), echo=lambda s: None)["lights_staged"] == 3


def test_keep_focuser_zero_flag_stacks_them(lib, tmp_path):
    msgs = []
    res = pl.run(_opts(lib, tmp_path, keep_focuser_zero=True), echo=msgs.append)
    assert res["lights_staged"] == 5 and not res["focuser_zero"]["excluded"]
    assert any("WARNING: 2 light(s) with FOCPOS 0 kept" in m for m in msgs)


def test_all_zero_is_an_error(tmp_path):
    L = tmp_path / "Library"
    for i in range(3):
        _fits(L / "M 31" / "L" / f"z{i}.fits", "LIGHT", 30.0, focpos=0, minute=i)
    with pytest.raises(pl.PipelineError, match="FOCPOS 0"):
        pl.run(_opts(L, tmp_path), echo=lambda s: None)
