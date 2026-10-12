"""PS-177: star QA and stacking per exposure group; PS-178: the integrate
calibration matcher explains alternatives and refuses a length with no dark.

The 2026-10-08 M31 RC16 dry run: 63 lights (B 30s x5, B 300s x1, G 30s x3,
L 30s x42, L 300s x6, R 30s x5, R 300s x1) were judged against ONE L 300 s
reference and 54 were flagged; no 300 s darks and no matching bias."""
import json
import math
from pathlib import Path

import numpy as np
import pytest

from photonscript.integration import calib
from photonscript.integration import pipeline as pl
from photonscript.integration import star_qa as q
from photonscript.integration.frames import Frame

W, H = 3000, 2000


def _field(n, seed):
    rng = np.random.default_rng(seed)
    return np.column_stack([rng.uniform(40, W - 40, n), rng.uniform(40, H - 40, n)])


def _row(i, xy, filt, exp, stars=None):
    return {"file": f"{filt}_{exp:g}_{i:03d}.fits", "filter": filt, "exp": exp, "time": f"T{i:03d}",
            "stars": stars or len(xy), "hfd_px": 3.5, "ecc": 0.45, "sky_adu": 1000.0,
            "double_frac": 0.0, "double_vec": "", "theta_R": 0.1, "xbin": 1, "readout": "HCG",
            "_xy": np.asarray(xy, np.float32), "_shape": (W, H)}


# ----------------------------------------------------------- star QA groups

def test_each_exposure_group_gets_its_own_reference_and_is_kept():
    deep = _field(900, seed=1)
    rows = [_row(i, deep + np.random.default_rng(i).normal(0, 0.2, deep.shape), "L", 300.0)
            for i in range(6)]
    # the 30 s subs: a field the 300 s reference cannot register (the old
    # single-reference QA called every one "not_registered"), few stars
    shallow = _field(120, seed=2)
    rows += [_row(10 + i, shallow + np.random.default_rng(50 + i).normal(0, 0.2, shallow.shape),
                  "L", 30.0) for i in range(12)]
    run_ref = q.assess(rows, q.Thresholds())
    assert rows[run_ref]["exp"] == 300.0 and rows[run_ref]["is_run_reference"]
    assert all(r["action"] == "keep" for r in rows), [r["reason"] for r in rows if r["action"] != "keep"]
    refs = {r["group"]: r["group_reference"] for r in rows}
    assert set(refs) == {"L 300 s HCG", "L 30 s HCG"}
    assert refs["L 30 s HCG"].startswith("L_30_") and refs["L 300 s HCG"].startswith("L_300_")
    assert sum(r["is_reference"] for r in rows) == 2


def test_groups_split_by_filter_binning_and_readout():
    a = {"filter": "L", "exp": 30.0, "xbin": 1, "readout": "HCG"}
    assert q.group_key(a) != q.group_key(dict(a, xbin=2))
    assert q.group_key(a) != q.group_key(dict(a, readout="LCG"))
    assert q.group_key(a) != q.group_key(dict(a, filter="R"))
    assert q.group_key({"filter": "L", "exp": 30.0}) == ("L", 30.0, 1, "")
    assert q.group_label(("L", 30.0, 2, "HCG")) == "L 30 s bin2 HCG"


def test_a_bad_sub_is_still_caught_inside_its_group():
    base = _field(500, seed=11)
    rows = [_row(i, base + np.random.default_rng(i).normal(0, 0.2, base.shape), "R", 30.0)
            for i in range(9)]
    rows[4]["stars"] = 100                         # clouded: low stars vs its 30 s neighbors
    q.assess(rows, q.Thresholds())
    assert rows[4]["action"] == "reject" and rows[4]["reason"].startswith("low_stars")
    assert sum(r["action"] == "reject" for r in rows) == 1


def test_group_summary_counts_and_orders_longest_first():
    rows = [{"file": "a", "filter": "L", "exp": 30.0, "action": "keep", "reason": "stars_ok",
             "group": "L 30 s", "group_reference": "a"},
            {"file": "b", "filter": "L", "exp": 30.0, "action": "reject",
             "reason": "soft(hfd 5 vs nb 3 px);trailed(ecc 0.8)", "group": "L 30 s",
             "group_reference": "a"},
            {"file": "c", "filter": "L", "exp": 300.0, "action": "keep", "reason": "stars_ok",
             "group": "L 300 s", "group_reference": "c"}]
    g = q.group_summary(rows)
    assert [x["group"] for x in g] == ["L 300 s", "L 30 s"]
    assert g[1] == {**g[1], "subs": 2, "keep": 1, "reject": 1,
                    "reasons": {"soft": 1, "trailed": 1}}


def test_csv_has_group_columns(tmp_path):
    rows = [{"file": "a", "filter": "L", "exp": 30.0, "action": "keep", "reason": "stars_ok",
             "group": "L 30 s", "group_reference": "a", "is_reference": True,
             "is_run_reference": False, "stars": 10, "hfd_px": math.nan}]
    q.write_csv(tmp_path / "x.csv", rows)
    head, line = (tmp_path / "x.csv").read_text().splitlines()
    cols = head.split(",")
    assert cols[7:11] == ["group", "group_reference", "is_reference", "is_run_reference"]
    assert line.split(",")[7] == "L 30 s"


# ----------------------------------------------------------- stacks per exposure

def _fr(name, filt, exp, osc=False):
    return Frame(path=Path(name), exp=exp, filter=filt, bayer="RGGB" if osc else "")


def _m31_lights():
    out = []
    for filt, exp, n in (("B", 30, 5), ("B", 300, 1), ("G", 30, 3), ("L", 30, 42),
                         ("L", 300, 6), ("R", 30, 5), ("R", 300, 1)):
        out += [_fr(f"{filt}_{exp}_{i}.fits", filt, float(exp)) for i in range(n)]
    return out


def test_plan_stacks_m31_one_length_per_stack():
    specs, left, notes = pl.plan_stacks(_m31_lights(), min_group=3)
    got = {s["name"]: (s["filter"], s["exps"]) for s in specs}
    assert got == {"B": ("B", [30.0]), "G": ("G", [30.0]), "L": ("L", [300.0]),
                   "L_30s": ("L", [30.0]), "R": ("R", [30.0])}
    assert sorted(f.name for f, _ in left) == ["B_300_0.fits", "R_300_0.fits"]
    assert any("fewer than 3" in n and n.startswith("left out B 300 s") for n in notes)
    assert any(n.startswith("L: 300 s x 6 -> master_L, 30 s x 42 -> master_L_30s") for n in notes)


def test_plan_stacks_keeps_osc_exposures_together():
    fs = [_fr(f"a{i}.fits", "OSC", 120.0, osc=True) for i in range(3)]
    fs += [_fr("b.fits", "OSC", 400.0, osc=True)]
    specs, left, _ = pl.plan_stacks(fs, min_group=3)
    assert specs == [{"name": "OSC", "filter": "OSC", "exps": [120.0, 400.0]}] and not left


def test_plan_stacks_primary_is_most_integration_time():
    fs = [_fr(f"s{i}.fits", "Ha", 60.0) for i in range(20)]       # 1200 s
    fs += [_fr(f"l{i}.fits", "Ha", 600.0) for i in range(3)]      # 1800 s
    specs, _, _ = pl.plan_stacks(fs)
    assert [s["name"] for s in specs] == ["Ha", "Ha_60s"]


# ----------------------------------------------------------- calibration matcher

def _c(name, kind, exp=0.0, ro="HCG", temp=0.0, session="2026-10-01", offset=256):
    return Frame(path=Path(name), kind=kind, exp=exp, gain=200, offset=offset, set_temp=temp,
                 readout=ro, instrument="AP26MC", filter="", session=session)


EP = calib.Epoch("AP26MC", 200, 256, 1, 0.0, "HCG")


def test_alternatives_explain_the_m31_gap():
    cals = [_c(f"d300_{i}", "DARK", 300.0) for i in range(6)]                        # too few
    cals += [_c(f"d30_{i}", "DARK", 30.0, session="2026-10-06") for i in range(30)]   # usable
    cals += [_c(f"d300lcg_{i}", "DARK", 300.0, ro="LCG", session="2026-04-06") for i in range(20)]
    cals += [_c(f"b_{i}", "BIAS", ro="LCG", session="2026-07-31") for i in range(50)]
    cals += [_c(f"bw_{i}", "BIAS", temp=20.0, session="2026-09-30") for i in range(50)]
    lights = [Frame(path=Path(f"L{i}.fits"), exp=300.0, gain=200, offset=256, set_temp=0.0,
                    readout="HCG", instrument="AP26MC", filter="L") for i in range(6)]
    p = calib.plan(lights, cals, use_flats=False)
    assert not p.bias and p.uncalibrated() == [300.0]
    assert p.bias_misses == ["2026-09-30 (50 frames): temperature 20 C",
                             "2026-07-31 (50 frames): readout LCG"]
    alt = p.darks[300.0].alternatives
    assert alt[0].startswith("6 x 300 s darks at this epoch: --min-darks 6")
    assert alt[1] == "300 s darks off this epoch: 2026-04-06 (20 x 300 s): readout LCG"
    assert alt[2].startswith("scale 30 s x 30 darks (optimizeDarks) once a bias at this epoch")
    assert "2026-09-30 (50 frames): temperature 20 C" in alt[2]
    assert any(n.startswith("  nearest alternative: 6 x 300 s") for n in p.notes)


def test_no_alternative_says_capture():
    lights = [Frame(path=Path("L.fits"), exp=300.0, gain=200, offset=256, set_temp=0.0,
                    readout="HCG", instrument="AP26MC", filter="L")]
    p = calib.plan(lights, [], use_flats=False)
    assert p.darks[300.0].alternatives[0].startswith("capture 300 s darks at AP26MC gain 200")


# ----------------------------------------------------------- pipeline refusal

def _fits(path, kind, exp, filt="L", ro="High Conversion Gain", minute=0):
    from astropy.io import fits
    h = fits.Header()
    h["IMAGETYP"] = kind
    h["EXPTIME"] = exp
    h["GAIN"] = 200
    h["OFFSET"] = 256
    h["SET-TEMP"] = 0.0
    h["READOUTM"] = ro
    h["INSTRUME"] = "AP26MC"
    h["XBINNING"] = 1
    if filt:
        h["FILTER"] = filt
    h["DATE-LOC"] = f"2026-10-06T03:{minute:02d}:00"
    h["DATE-OBS"] = f"2026-10-06T09:{minute:02d}:00"
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((8, 8), np.uint16), header=h).writeto(path)


@pytest.fixture
def mono_lib(tmp_path):
    L = tmp_path / "Library"
    for i in range(4):
        _fits(L / "M 31" / "L" / f"2026-10-06_03-{i:02d}-00__L_300.00s_{i:04d}.fits", "LIGHT", 300.0,
              minute=i)
    for i in range(5):
        _fits(L / "M 31" / "L" / f"2026-10-06_03-{10 + i:02d}-00__L_30.00s_{i:04d}.fits", "LIGHT", 30.0,
              minute=10 + i)
    _fits(L / "M 31" / "R" / "2026-10-06_03-30-00__R_300.00s_0000.fits", "LIGHT", 300.0, filt="R",
          minute=30)
    for i in range(12):
        _fits(L / "Calibration" / "DARK" / "2026-10-06" / f"d30_{i:02d}.fits", "DARK", 30.0, filt="")
    return L


def _opts(lib, tmp_path, **kw):
    o = pl.Options(target="M31", rig="rc16", library=lib, staging_root=tmp_path / "Staging",
                   qa="off", run_pixinsight=False, flats=False, default_readout="HCG",
                   pixinsight=str(tmp_path / "PI" / "bin" / "PixInsight.exe"))
    for k, v in kw.items():
        setattr(o, k, v)
    return o


def test_pipeline_refuses_the_length_without_a_dark(mono_lib, tmp_path):
    msgs = []
    out = tmp_path / "Staging" / "r1"
    res = pl.run(_opts(mono_lib, tmp_path, out=out), echo=msgs.append)
    assert res["lights_staged"] == 5                     # only the 30 s L subs
    assert res["refused_uncalibrated"][0]["exp"] == 300.0
    assert res["refused_uncalibrated"][0]["n"] == 5
    assert any(m.strip().startswith("REFUSED: 5 x 300 s lights (L, R) have no dark") for m in msgs)
    js = (out / "integrate_run.js").read_text()
    cfg = json.loads(js.split("var CONFIG = ", 1)[1].split(";\n", 1)[0])
    assert [s["name"] for s in cfg["stacks"]] == ["L"]
    assert cfg["stacks"][0]["groups"][0]["exp"] == 30.0
    assert cfg["stacks"][0]["mixed_exposures"] is False
    assert not (out / "LIGHTS" / "L" / "300s").exists()


def test_pipeline_allow_uncalibrated_splits_lengths(mono_lib, tmp_path):
    msgs = []
    out = tmp_path / "Staging" / "r2"
    res = pl.run(_opts(mono_lib, tmp_path, out=out, allow_uncalibrated=True), echo=msgs.append)
    assert res["refused_uncalibrated"] == []
    names = [s["name"] for s in res["stacks"]]
    assert names == ["L", "L_30s"]                        # 300 s x4 (1200 s) beats 30 s x5
    assert all(len(s["groups"]) == 1 for s in res["stacks"])
    assert res["lights_staged"] == 9                     # R 300 s x1 left out (fewer than 3)
    assert any("left out R 300 s: 1 sub(s)" in m for m in msgs)
    assert any("WITHOUT a dark" in m for m in msgs)


def test_pipeline_refuses_everything_names_the_flag(mono_lib, tmp_path):
    with pytest.raises(pl.PipelineError, match="--allow-uncalibrated"):
        pl.run(_opts(mono_lib, tmp_path, out=tmp_path / "S" / "x", filters=["R"]),
               echo=lambda s: None)


def test_pjsr_never_integrates_mixed_lengths_with_equal_weights():
    from photonscript.integration import pjsr
    t = pjsr.template(pjsr.INTEGRATE_TEMPLATE)
    assert "stack.mixed_exposures ? ImageIntegration.prototype.ExposureTimeWeight" in t
    t.encode("ascii")


# ----------------------------------------------------------- mono hot pixels

def test_mono_detect_ignores_hot_pixels():
    """PS-177: raw RC16 subs gave sep the hot pixels (HFD 0.8 px, 4000 cap),
    so every sub registered on the sensor pattern. Mono frames are 3x3
    median filtered first; the stars survive, single hot pixels do not."""
    pytest.importorskip("sep")
    rng = np.random.default_rng(3)
    img = rng.normal(300.0, 5.0, (600, 800)).astype(np.float32)
    yy, xx = np.mgrid[0:600, 0:800]
    stars = [(100 + 60 * i, 80 + 45 * j) for i in range(10) for j in range(10)]
    for x, y in stars:
        img += 3000.0 * np.exp(-((xx - x) ** 2 + (yy - y) ** 2) / (2 * 3.0 ** 2))
    hx, hy = rng.integers(5, 795, 3000), rng.integers(5, 595, 3000)
    img[hy, hx] = 20000.0                                  # isolated hot pixels
    st = q.detect(img, osc=False)
    assert 90 <= len(st["x"]) <= 110
    assert np.median(st["hfd"]) > 3.0


# ----------------------------------------- one tiny / failed group never aborts the run
# 2026-10-12 M31 rc16: B_300s had 3 subs, only 2 registered inside PixInsight,
# and the throw stopped the run before the L 30 s, R and G masters.

def _stack_js():
    from photonscript.integration import pjsr
    return pjsr.template(pjsr.INTEGRATE_TEMPLATE)


def test_pjsr_skips_a_stack_with_too_few_registered_frames():
    t = _stack_js()
    assert "var MIN_FRAMES = Math.max(3, CONFIG.min_group_frames || 3);" in t
    assert "if (regFiles.length < MIN_FRAMES)" in t
    assert "too few registered frames" in t and "too few frames after local normalization" in t
    main = t[t.index("function main()"):]
    loop = main[main.index("for (var s = 0; s < CONFIG.stacks.length; ++s)"):main.index("if (!made)")]
    assert loop.index("try {") < loop.index("runStack(CONFIG.stacks[s], masters, reg)")
    assert "catch (e)" in loop and "skipStack(SN," in loop
    # the run fails only when no stack made a master
    assert 'if (!made) throw new Error("no stack produced a master");' in main


def test_pjsr_skip_logs_records_and_cleans_up():
    t = _stack_js()
    sk = t[t.index("function skipStack("):t.index("function main()")]
    assert 'log("SKIPPED [" + S + "]: " + why + " (no master_" + S' in sk
    assert 'TIMING.push(S + ",SKIPPED,"' in sk and "writeText(TIMINGFILE, TIMING)" in sk
    assert 'TEND(S, "stack total")' in sk and '"integration"' in sk and "forceClose()" in sk
    for n, ln in enumerate(t.splitlines(), 1):
        if "//" in ln:
            assert "/*" not in ln.split("//", 1)[1], n
    t.encode("ascii")


def test_integrate_config_carries_min_group_frames(mono_lib, tmp_path):
    out = tmp_path / "Staging" / "mg"
    pl.run(_opts(mono_lib, tmp_path, out=out, allow_uncalibrated=True, min_group_frames=4),
           echo=lambda s: None)
    js = (out / "integrate_run.js").read_text()
    cfg = json.loads(js.split("var CONFIG = ", 1)[1].split(";\n", 1)[0])
    assert cfg["min_group_frames"] == 4
    from photonscript.integration import pjsr
    assert pjsr.check(js) == []
    # a mono run never renders the OSC palette
    assert 'var PALETTE  = "off";' in (out / "finish_run.js").read_text()


def test_skipped_from_log():
    txt = ("2026-10-12T05:00:00Z  [+3.1m]  SKIPPED [B_300s]: [B_300s] too few registered frames "
           "(2, need 3) (no master_B_300s; continuing with the other stacks)\n"
           "x  SKIPPED [R]: integration failed (no master_R; continuing with the other stacks)\n"
           "x  SKIPPED [R]: again (no master_R; continuing)\n")
    assert pl.skipped_from_log(txt) == [
        {"stack": "B_300s", "reason": "[B_300s] too few registered frames (2, need 3)"},
        {"stack": "R", "reason": "integration failed"}]
    assert pl.skipped_from_log("EXIT OK\n") == []


def test_pipeline_reports_a_skipped_stack_and_finishes_the_rest(mono_lib, tmp_path, monkeypatch):
    out = tmp_path / "Staging" / "skip"
    calls = []

    def fake_run(script, log_path, **kw):
        script, log_path = Path(script), Path(log_path)
        calls.append(script.name)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        if script.name == "integrate_run.js":
            (log_path.parent / "master").mkdir(parents=True, exist_ok=True)
            (log_path.parent / "master" / "master_L.xisf").write_bytes(b"x")
            log_path.write_text(
                "t  [L] master saved\n"
                "t  SKIPPED [L_30s]: [L_30s] too few registered frames (2, need 3) "
                "(no master_L_30s; continuing with the other stacks)\n"
                "t  stacks SKIPPED: 1 of 2 (L_30s)\nt  EXIT OK\n")
        else:
            log_path.write_text("t  EXIT OK\n")
        return {"ok": True, "exit_code": 0, "minutes": 0.1, "last_line": "EXIT OK", "timed_out": False}

    monkeypatch.setattr(pl.runner, "run_script", fake_run)
    msgs = []
    res = pl.run(_opts(mono_lib, tmp_path, out=out, allow_uncalibrated=True, run_pixinsight=True),
                 echo=msgs.append)
    assert calls == ["integrate_run.js", "finish_run.js"]          # the run went on
    want = [{"stack": "L_30s", "reason": "[L_30s] too few registered frames (2, need 3)"}]
    assert res["skipped_stacks"] == want and res["integration"]["skipped_stacks"] == want
    assert res["integration"]["ok"] and res["finish"]["ok"]
    assert any("SKIPPED stack L_30s" in m for m in msgs)
    fin = (out / "finish_run.js").read_text()
    masters = json.loads(fin.split("var MASTERS = ", 1)[1].split(";\n", 1)[0])
    assert [m["name"] for m in masters] == ["M31_L"]               # only what exists
    assert any(r[0] == "PixInsight stack L_30s SKIPPED" and r[4] == "skipped" for r in res["timing"])
    led = json.loads(Path(res["ledger"]).read_text())
    assert led["machine"]["integration"]["skipped_stacks"] == want
    used = {s["file"] for s in led["machine"]["subs"] if s["used"]}
    assert used and all("_300.00s_" in f for f in used)            # the 30 s subs were not integrated
    md = next((out / "astrobin").glob("*_packet.md")).read_text()
    assert "Stack L_30s skipped inside PixInsight" in md
