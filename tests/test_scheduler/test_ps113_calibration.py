"""PS-113: calibration frame QA + quarantine, the needs-vs-have gap report,
the guarded capture job, the day vs night leak check and readiness counting
only QA-passed frames. Everything runs on synthetic FITS in tmp dirs (never
the ninashare mirror) with a fake NINA."""

import asyncio
import json
import zlib
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from photonscript.scheduler import calibration_capture as cc
from photonscript.scheduler import calibration_plan as cp
from photonscript.scheduler import calibration_qa as cq
from photonscript.scheduler import readiness, runs
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)

_REAL_RIGIO = cc.RigIO


def _ago(days):
    from datetime import timedelta
    return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")


D1, D2, D3, D4 = _ago(22), _ago(13), _ago(12), _ago(0)  # Library folder dates
NIGHT = "2026-09-12T04:00:00"      # sun far below the horizon at AARO
DAY = "2026-09-12T19:00:00"        # 13:00 MDT, sun up


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=tmp_path / "data",
                library_dir=str(tmp_path / "lib"),
                image_watch_dir=str(tmp_path / "watch"),
                nina_logs_dir=str(tmp_path / "logs"))
    base.update(kw)
    return PhotonScriptConfig(**base)


def _pb_cfg(tmp_path, **kw):
    return _cfg(tmp_path, piggyback_enabled=True,
                piggyback_image_watch_dir=str(tmp_path / "pbwatch"), **kw)


def write_frame(path: Path, *, typ="DARK", exp=120.0, gain=100, offset=256,
                settemp=0.0, ccd=0.0, level=261.0, noise=3.0, shape=(128, 192),
                leak=0.0, stars=0, date_obs=NIGHT, instrume="AP26CC", filt=None,
                vignette=0.0, seed=0):
    rng = np.random.default_rng(seed)
    h, w = shape
    data = rng.normal(level, noise, shape)
    yy, xx = np.mgrid[0:h, 0:w]
    if leak:
        data += leak * yy / h
    if vignette:
        r2 = ((yy - h / 2) ** 2 + (xx - w / 2) ** 2) / ((h / 2) ** 2 + (w / 2) ** 2)
        data *= 1 - vignette * r2
    for i in range(stars):
        cy, cx = rng.uniform(8, h - 8), rng.uniform(8, w - 8)
        data += 3000 * np.exp(-((yy - cy) ** 2 + (xx - cx) ** 2) / (2 * 3.0 ** 2))
    hdr = fits.Header()
    hdr["IMAGETYP"] = typ
    hdr["EXPTIME"] = exp
    hdr["GAIN"] = gain
    hdr["OFFSET"] = offset
    if settemp is not None:
        hdr["SET-TEMP"] = settemp
    if ccd is not None:
        hdr["CCD-TEMP"] = ccd
    hdr["XBINNING"] = 1
    hdr["INSTRUME"] = instrume
    hdr["DATE-OBS"] = date_obs
    if filt:
        hdr["FILTER"] = filt
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.clip(data, 0, 65535).astype(np.uint16), hdr).writeto(path)
    return path


def lib_set(lib: Path, typ: str, date: str, n: int, prefix="f", **kw):
    out = []
    for i in range(n):
        out.append(write_frame(lib / "Calibration" / typ / date / f"{prefix}{i:03d}.fits",
                               typ=typ, seed=zlib.crc32(f"{date}{prefix}{i}".encode()), **kw))
    return out


# --- per-frame judging ---------------------------------------------------------------

def _rec(**kw):
    r = {"type": "DARK", "imagetyp": "DARK", "exptime": 120.0, "gain": 100,
         "offset": 256, "settemp": 0.0, "ccdtemp": 0.0, "xbin": 1, "median": 262.0,
         "mad": 3.0, "center": 261.0, "corners": [261.0, 261.2, 260.8, 261.0],
         "stars": 0}
    r.update(kw)
    return r


def codes(fails):
    return sorted({f["code"] for f in fails})


def test_judge_dark_rules():
    ref = (256.0, "bias test")
    assert cq.judge_frame(_rec(), rig="piggyback", bias_ref=ref) == ([], [])
    # the 2026-09-21 uncooled darks
    f, _ = cq.judge_frame(_rec(ccdtemp=35.7, median=309.0), rig="piggyback", bias_ref=ref)
    assert "temp" in codes(f) and "level" in codes(f)
    f, _ = cq.judge_frame(_rec(ccdtemp=None), rig="piggyback", bias_ref=ref)
    assert codes(f) == ["temp"]
    f, _ = cq.judge_frame(_rec(ccdtemp=0.9), rig="piggyback", bias_ref=ref)
    assert f == []
    f, _ = cq.judge_frame(_rec(median=250.0), rig="piggyback", bias_ref=ref)
    assert codes(f) == ["level"]
    f, _ = cq.judge_frame(_rec(corners=[268.0, 261, 261, 261]), rig="piggyback",
                          bias_ref=ref)
    assert codes(f) == ["leak"]
    f, _ = cq.judge_frame(_rec(stars=126), rig="piggyback", bias_ref=ref)
    assert codes(f) == ["stars"]
    f, _ = cq.judge_frame(_rec(imagetyp="LIGHT"), rig="piggyback", bias_ref=ref)
    assert codes(f) == ["header"]
    f, _ = cq.judge_frame(_rec(expect={"gain": 100, "offset": 256, "xbin": 1,
                                       "exposures": [300.0]}),
                          rig="piggyback", bias_ref=ref)
    assert codes(f) == ["header"]
    # 600 s at 0 C: +7 ADU is inside base 10 + 0.02 x 600
    f, _ = cq.judge_frame(_rec(exptime=600.0, median=261.0), rig="rc16",
                          bias_ref=(254.0, "b"))
    assert f == []


def test_judge_bias_and_flats():
    f, _ = cq.judge_frame(_rec(type="BIAS", imagetyp="BIAS", exptime=0.001,
                               median=256.0), rig="piggyback")
    assert f == []
    f, _ = cq.judge_frame(_rec(type="BIAS", imagetyp="BIAS", exptime=1.0),
                          rig="piggyback")
    assert codes(f) == ["header"]
    flat = dict(type="FLAT", imagetyp="FLAT", exptime=0.5, median=33000.0,
                center=36000.0, corners=[29000.0] * 4, sat_pct=0.0, ccdtemp=39.0)
    f, w = cq.judge_frame(_rec(**flat), rig="piggyback")
    assert f == [] and codes(w) == ["temp"]  # flats: temperature is a warning
    f, _ = cq.judge_frame(_rec(**{**flat, "median": 64000.0}), rig="piggyback")
    assert codes(f) == ["flat_level"]
    f, _ = cq.judge_frame(_rec(**{**flat, "sat_pct": 2.0}), rig="piggyback")
    assert codes(f) == ["saturated"]
    nov = {**flat, "corners": [36000.0] * 4}
    f, _ = cq.judge_frame(_rec(**nov), rig="piggyback")
    assert codes(f) == ["vignetting"]
    f, w = cq.judge_frame(_rec(**nov), rig="rc16")      # RC16: warning only
    assert f == [] and "vignetting" in codes(w)


# --- measuring + backfill + quarantine -----------------------------------------------------

def _piggy_library(tmp_path):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    for i in range(6):
        write_frame(lib / "Calibration" / "BIAS" / D1 / f"b{i}.fits",
                    typ="BIAS", exp=0.001, level=256.0, seed=100 + i)
    good = lib_set(lib, "DARK", D1, 6, prefix="g", level=261.0)
    warm = lib_set(lib, "DARK", D2, 5, prefix="w", ccd=40.0, level=380.0)
    leak = write_frame(lib / "Calibration" / "DARK" / D1 / "leak.fits",
                       leak=24.0, level=261.0, seed=7)
    starry = write_frame(lib / "Calibration" / "DARK" / D1 / "stars.fits",
                         stars=60, shape=(256, 384), level=261.0, seed=8)
    return cfg, lib, good, warm, leak, starry


def test_backfill_dry_run_then_real(tmp_path):
    cfg, lib, good, warm, leak, starry = _piggy_library(tmp_path)
    before = sorted(str(p) for p in lib.rglob("*"))
    rep = cq.backfill(cfg, "piggyback", dry_run=True)
    r = rep["rigs"]["piggyback"]
    assert sorted(str(p) for p in lib.rglob("*")) == before   # nothing moved
    assert r["fail"] == 7 and r["pass"] == 12
    assert len(r["would_quarantine"]) == 7 and not r["quarantined"]
    store = cq.load_store(cfg, "piggyback")["frames"]
    w = store[f"DARK/{D2}/w000.fits"]
    assert w["verdict"] == "fail" and "temp" in w["codes"]
    assert "leak" in store[f"DARK/{D1}/leak.fits"]["codes"]
    assert "stars" in store[f"DARK/{D1}/stars.fits"]["codes"]
    assert store[f"DARK/{D1}/g000.fits"]["verdict"] == "pass"
    # the night dark quota skips QA-failed frames still on disk (13 -> 6)
    from photonscript.scheduler.calibration import count_matching_darks
    view = cq.rig_view(cfg, "piggyback")
    assert count_matching_darks(view, 120.0, gain=100, offset=256, setpoint=0.0) == 6
    off = cq.rig_view(_pb_cfg(tmp_path, calibration_qa_mode="off"), "piggyback")
    assert count_matching_darks(off, 120.0, gain=100, offset=256, setpoint=0.0) == 13
    # the daytime switch is left alone by a dry run
    assert cq.daytime_state(cfg, "piggyback") == {"status": "untested"}

    rep = cq.backfill(cfg, "piggyback")
    r = rep["rigs"]["piggyback"]
    assert len(r["quarantined"]) == 7
    q = lib / "Calibration" / "_quarantine" / "DARK" / D2
    assert sorted(p.name for p in q.glob("*.fits")) == [p.name for p in warm]
    reasons = json.loads((q / "reasons.json").read_text())
    assert any("CCD-TEMP 40" in x for x in reasons["w000.fits"]["reasons"])
    assert not (lib / "Calibration" / "DARK" / D2).exists()
    assert all(p.exists() for p in good)
    # the calibration scan and the dark count no longer see them
    from photonscript.scheduler.calibration import iter_calibration_frames
    names = {f.name for _t, _d, f in iter_calibration_frames(cq.rig_view(cfg, "piggyback"))}
    assert "w000.fits" not in names and "g000.fits" in names
    assert cq.count_passed_darks(cfg, "piggyback", 120.0, gain=100, offset=256,
                                 setpoint=0.0) == 6
    # the night dark quota no longer counts them either (watch-dir copies too)
    from photonscript.scheduler.calibration import count_matching_darks
    assert count_matching_darks(cq.rig_view(cfg, "piggyback"), 120.0, gain=100,
                                offset=256, setpoint=0.0) == 6

    # restore = false-positive escape hatch
    res = cq.restore(cfg, "piggyback")
    assert len(res["restored"]) == 7
    assert (lib / "Calibration" / "DARK" / D2 / "w000.fits").exists()
    assert cq.load_store(cfg, "piggyback")["frames"][f"DARK/{D2}/w000.fits"][
        "verdict"] == "pass"


def test_set_outlier(tmp_path):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    lib_set(lib, "DARK", D3, 6, level=261.0)
    write_frame(lib / "Calibration" / "DARK" / D3 / "odd.fits",
                level=268.0, seed=99)  # inside the level band, off the set
    rep = cq.backfill(cfg, "piggyback", dry_run=True)
    rec = cq.load_store(cfg, "piggyback")["frames"][f"DARK/{D3}/odd.fits"]
    assert rec["codes"] == ["outlier"], rec["reasons"]
    assert rep["rigs"]["piggyback"]["fail"] == 1


def test_filing_hook_quarantines_new_frames(tmp_path):
    cfg = _cfg(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    watch = Path(cfg.image_watch_dir) / today
    for i in range(5):
        write_frame(watch / "BIAS" / f"b{i}.fits", typ="BIAS", exp=0.001, gain=200,
                    level=254.0, instrume="AP26MC", seed=i)
    for i in range(5):
        write_frame(watch / "DARK" / f"d{i}.fits", exp=180.0, gain=200, level=256.0,
                    instrume="AP26MC", seed=20 + i)
    write_frame(watch / "DARK" / "hot.fits", exp=180.0, gain=200, ccd=36.0,
                level=520.0, instrume="AP26MC", seed=30)
    lib = runs.library_root(cfg)
    linked, _ = runs._link_calibration_night(cfg, Path(cfg.image_watch_dir), lib, today)
    assert linked == 10
    assert not (lib / "Calibration" / "DARK" / today / "hot.fits").exists()
    q = lib / "Calibration" / "_quarantine" / "DARK" / today / "hot.fits"
    assert q.exists() and (watch / "DARK" / "hot.fits").exists()  # original kept
    # a second pass is idempotent
    assert runs._link_calibration_night(cfg, Path(cfg.image_watch_dir), lib, today) == (0, 10)


def test_filing_modes_report_and_off(tmp_path):
    today = datetime.now().strftime("%Y-%m-%d")
    for mode, has_store in (("report", True), ("off", False)):
        root = tmp_path / mode
        cfg = _cfg(root, calibration_qa_mode=mode)
        watch = Path(cfg.image_watch_dir) / today
        write_frame(watch / "DARK" / "hot.fits", exp=180.0, gain=200, ccd=36.0,
                    level=520.0, instrume="AP26MC")
        lib = runs.library_root(cfg)
        runs._link_calibration_night(cfg, Path(cfg.image_watch_dir), lib, today)
        assert (lib / "Calibration" / "DARK" / today / "hot.fits").exists()
        assert cq.has_store(cfg, "rc16") is has_store


def test_filing_survives_unreadable_frames(tmp_path):
    cfg = _cfg(tmp_path)
    today = datetime.now().strftime("%Y-%m-%d")
    f = Path(cfg.image_watch_dir) / today / "DARK" / "junk.fits"
    f.parent.mkdir(parents=True)
    f.write_bytes(b"x")
    lib = runs.library_root(cfg)
    assert runs._link_calibration_night(cfg, Path(cfg.image_watch_dir), lib, today) == (1, 0)


# --- day vs night ------------------------------------------------------------------------------

def test_daytime_leak_disables_daytime_capture(tmp_path):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    lib_set(lib, "DARK", D1, 5, prefix="n", level=261.0, date_obs=NIGHT)
    lib_set(lib, "DARK", D4, 3, prefix="d", level=268.0, date_obs=DAY)
    cq.backfill(cfg, "piggyback")
    st = cq.daytime_state(cfg, "piggyback")
    assert st["status"] == "disabled"
    assert not cq.daytime_capture_allowed(cfg, "piggyback")
    recs = cq.load_store(cfg, "piggyback")["frames"]
    assert "daytime_leak" in recs[f"DARK/{D4}/d000.fits"]["codes"]
    assert recs[f"DARK/{D1}/n000.fits"]["verdict"] == "pass"
    # sticky until reset by hand
    cq.backfill(cfg, "piggyback")
    assert cq.daytime_state(cfg, "piggyback")["status"] == "disabled"
    cq.set_daytime_state(cfg, "piggyback", "untested")
    assert cq.daytime_capture_allowed(cfg, "piggyback")


def test_clean_daytime_darks_mark_daytime_ok(tmp_path):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    lib_set(lib, "DARK", D1, 5, prefix="n", level=261.0, date_obs=NIGHT)
    lib_set(lib, "DARK", D4, 3, prefix="d", level=261.5, date_obs=DAY)
    cq.backfill(cfg, "piggyback")
    assert cq.daytime_state(cfg, "piggyback")["status"] == "ok"


# --- readiness counts only QA-passed frames -------------------------------------------------

def test_readiness_counts_only_passed(tmp_path):
    cfg, lib, *_ = _piggy_library(tmp_path)
    legacy = readiness.calibration_context(cfg, "piggyback")
    assert legacy.qa == ""
    cq.backfill(cfg, "piggyback", dry_run=True)
    ctx = readiness.calibration_context(cfg, "piggyback")
    assert ctx.qa == "passed"
    assert ctx.darks(120.0) == 6          # the 5 warm + leak + stars excluded
    assert ctx.bias == 6
    # rc16 has no store: unchanged header count
    assert readiness.calibration_context(cfg).qa == ""


# --- gap report --------------------------------------------------------------------------------

def _m31(rig="piggyback"):
    return ImagingProject(
        id="m31", active=True,
        target=CelestialTarget(name="M 31", ra_hours=0.71, dec_degrees=41.27),
        exposure_plans=[ExposurePlan(filter_type=FilterType.OSC, exposure_seconds=400,
                                     count=40, rig=rig)])


def test_gap_report_piggyback(tmp_path):
    cfg, lib, *_ = _piggy_library(tmp_path)
    main = runs.library_root(cfg)
    # 300 s M31 subs in the Library at the OSC epoch (lights source)
    for i in range(2):
        write_frame(main / "M 31" / "OSC" / f"l{i}.fits", typ="LIGHT", exp=300.0)
    cq.backfill(cfg, "piggyback", dry_run=True)
    rep = cp.gap_report(cfg, "piggyback", projects=[_m31()])
    r = rep["rigs"][0]
    by = {s["label"]: s for s in r["sets"]}
    assert set(by) >= {"dark 120 s", "dark 300 s", "dark 400 s", "bias", "flat OSC"}
    assert by["dark 120 s"]["have"] == 6 and by["dark 120 s"]["gap"] == 24
    assert by["dark 120 s"]["bad"] == 7
    assert by["dark 400 s"]["have"] == 0 and by["dark 400 s"]["gap"] == 30
    assert any("lights M 31" in s for s in by["dark 300 s"]["sources"])
    assert by["flat OSC"]["capturable"] is False
    assert by["dark 400 s"]["text"].endswith("0 of 30")
    cap = r["capture"]
    assert [e for e, _n in cap["darks"]][:2] == [300.0, 400.0]
    assert "Piggy-600" in cp.format_report(rep)


def test_rc16_needs_include_cap_hdr_and_tonight(tmp_path):
    cfg = _cfg(tmp_path)
    cat = ImagingProject(
        id="cat", active=True,
        target=CelestialTarget(name="Cat's Eye Nebula", ra_hours=17.98, dec_degrees=66.6),
        exposure_plans=[ExposurePlan(filter_type=FilterType("Ha"), exposure_seconds=600,
                                     count=20, hdr_short_seconds=60, hdr_short_count=20)])
    rd = runs.runs_dir(cfg)
    night = datetime.now().strftime("%Y-%m-%d")
    (rd / f"{night}_plan.json").write_text(json.dumps({
        "night_of": night, "targets": [{"name": "X", "exposures": [
            {"filter": "L", "exp_s": 240.0, "planned": 5}]}]}))
    needs = {s["exp_s"] for s in cp.rig_needs(cfg, "rc16", [cat]) if s["type"] == "DARK"}
    assert needs == {600.0, 180.0, 300.0, 60.0, 240.0}


def test_capture_plan_budget_and_order():
    sets = [{"type": "DARK", "exp_s": 600.0, "need": 30, "gap": 10, "capturable": True},
            {"type": "DARK", "exp_s": 300.0, "need": 30, "gap": 30, "capturable": True},
            {"type": "BIAS", "exp_s": 0.001, "need": 50, "gap": 0, "capturable": True},
            {"type": "FLAT", "exp_s": None, "need": 15, "gap": 15, "capturable": False}]
    p = cp.capture_plan(sets)
    assert p["darks"] == [[300.0, 30], [600.0, 10]] and p["bias"] == 0
    p = cp.capture_plan(sets, budget_min=60)
    assert p["trimmed"] and p["darks"] == [[300.0, 11]]


# --- the capture job ----------------------------------------------------------------------------

class FakeIO:
    """A fake NINA for one rig: temperatures to report, the roof, the
    sequence state; dispatch makes frames 'land' one per camera read."""

    def __init__(self, cfg, rig="piggyback", temps=None, roof=True, running=False,
                 guider="", frame_kw=None):
        self.cfg, self.rig = cfg, rig
        self.temps = list(temps or [8.0, 0.4, 0.1])
        self.roof_closed = roof
        self.running = running
        self.guider = guider
        self.calls = []
        self.seq = None
        self.to_land = []
        self.frame_kw = frame_kw or {}
        self.on_camera = None

    async def connect_camera(self):
        self.calls.append("connect_camera")
        return True, ""

    async def camera(self):
        self.calls.append("camera")
        if self.on_camera:
            self.on_camera(self)
        if self.seq is not None and self.to_land:
            typ, exp, i = self.to_land.pop(0)
            day = datetime.now().strftime("%Y-%m-%d")
            watch = Path(cq.rig_view(self.cfg, self.rig).image_watch_dir)
            kw = dict(typ=typ, exp=exp, level=256.0 if typ == "BIAS" else 261.0,
                      seed=500 + i)
            kw.update(self.frame_kw)
            write_frame(watch / day / typ / f"{typ}_{exp:g}_{i:03d}.fits", **kw)
            if not self.to_land:
                self.running = False
        t = self.temps.pop(0) if len(self.temps) > 1 else self.temps[0]
        return {"Temperature": t, "Connected": True}

    async def cool(self, sp):
        self.calls.append(("cool", sp))
        return {"ok": True}

    async def warm(self):
        self.calls.append("warm")
        return {"ok": True}

    async def stop(self):
        self.calls.append("stop")
        self.running = False
        return {"ok": True}

    async def dispatch(self, seq):
        self.calls.append("dispatch")
        self.seq = seq
        self.running = True
        n = 0
        for blk in _blocks(seq):
            for i in range(blk[2]):
                self.to_land.append((blk[0], blk[1], n))
                n += 1
        return {"ok": True, "detail": "loaded + started"}

    async def sequence_running(self):
        return self.running

    async def roof(self, connect=True):
        return self.roof_closed, "fake monitor"

    async def guider_state(self):
        return self.guider


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _blocks(seq):
    out = []
    for c in _walk(seq):
        items = (c.get("Items") or {}).get("$values") if isinstance(c, dict) else None
        conds = (c.get("Conditions") or {}).get("$values") if isinstance(c, dict) else None
        if not items or not conds:
            continue
        te = [i for i in items if "TakeExposure" in str(i.get("$type", ""))]
        loop = [x for x in conds if "LoopCondition" in str(x.get("$type", ""))]
        if te and loop:
            out.append((te[0]["ImageType"], te[0]["ExposureTime"], loop[0]["Iterations"]))
    return out


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _fast(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)   # the job saves its sequence under ./sequences
    monkeypatch.setattr(cc, "_jobs", {})
    monkeypatch.setattr(cc, "_autofill_done", {})

    async def _quiet(*a, **k):
        return True
    import photonscript.shared.pushover as po
    monkeypatch.setattr(po, "notify", _quiet)

    def _no_real_io(*a, **k):
        raise AssertionError("a test reached the real NINA")
    monkeypatch.setattr(cc, "RigIO", _no_real_io)


async def _start_and_wait(cfg, io, state="DISARMED", timeout=20, **kw):
    states = {"s": state}
    ok, body = await cc.start_job(cfg, io.rig, armer_state_fn=lambda: states["s"],
                                  io=io, poll_s=0.01, daytime=False, **kw)
    if not ok:
        return ok, body, None
    job = cc._jobs[io.rig]
    await asyncio.wait_for(job.task, timeout)
    return ok, body, job


@pytest.mark.parametrize("state", ["ARMED", "RUNNING", "PAUSED_UNSAFE"])
def test_refused_unless_disarmed_or_complete(tmp_path, state):
    cfg = _pb_cfg(tmp_path)
    io = FakeIO(cfg)
    ok, body, _ = _run(_start_and_wait(cfg, io, state=state, exposures=[300.0], count=2))
    assert not ok and any("armer is " + state in r for r in body["refusals"])
    assert "dispatch" not in io.calls and ("cool", 0.0) not in io.calls


@pytest.mark.parametrize("roof,needle", [(False, "roof reads OPEN"),
                                         (None, "roof state unknown")])
def test_refused_when_roof_not_closed(tmp_path, roof, needle):
    cfg = _pb_cfg(tmp_path)
    io = FakeIO(cfg, roof=roof)
    ok, body, _ = _run(_start_and_wait(cfg, io, exposures=[300.0], count=2))
    assert not ok and any(needle in r for r in body["refusals"])


def test_refused_when_nina_busy_or_phd2_active(tmp_path):
    cfg = _pb_cfg(tmp_path)
    ok, body, _ = _run(_start_and_wait(cfg, FakeIO(cfg, running=True),
                                       exposures=[300.0], count=2))
    assert not ok and any("running a sequence" in r for r in body["refusals"])
    ok, body, _ = _run(_start_and_wait(cfg, FakeIO(cfg, rig="rc16", guider="guiding"),
                                       exposures=[300.0], count=2))
    assert not ok and any("PHD2 is guiding" in r for r in body["refusals"])

    from photonscript.telescope_agent import phd2_ops

    async def held():
        async with phd2_ops.hold("selftest"):
            return await _start_and_wait(cfg, FakeIO(cfg, rig="rc16"),
                                         exposures=[300.0], count=2)
    ok, body, _ = _run(held())
    assert not ok and any("holds PHD2 (selftest)" in r for r in body["refusals"])


def test_refused_in_daytime_when_disabled(tmp_path):
    cfg = _pb_cfg(tmp_path)
    cq.set_daytime_state(cfg, "piggyback", "disabled")

    async def go():
        return await cc.start_job(cfg, "piggyback", armer_state_fn=lambda: "DISARMED",
                                  io=FakeIO(cfg), poll_s=0.01, daytime=True,
                                  exposures=[300.0], count=2)
    ok, body = _run(go())
    assert not ok and any("daytime capture is DISABLED" in r for r in body["refusals"])


def test_happy_path_piggyback(tmp_path):
    cfg = _pb_cfg(tmp_path)
    io = FakeIO(cfg)
    ok, body, job = _run(_start_and_wait(cfg, io, state="COMPLETE",
                                         exposures=[300.0, 400.0], count=3, bias=2))
    assert ok, body
    assert job.state == "complete", job.events
    # cooled and confirmed BEFORE the dispatch
    assert io.calls.index(("cool", 0.0)) < io.calls.index("dispatch")
    blocks = _blocks(io.seq)
    assert blocks == [("DARK", 300.0, 3), ("DARK", 400.0, 3), ("BIAS", 0.001, 2)]
    text = json.dumps(io.seq)
    for bad in ("Mount", "Slew", "Park", "Unpark", "Guiding", "Dome", "SwitchFilter",
                "Center"):
        assert bad not in text, bad
    assert "LoopWhileUnsafe" in text
    gains = {c.get("Gain") for c in _walk(io.seq) if "TakeExposure" in str(c.get("$type"))}
    assert gains == {100}           # the OSC gain, not the RC16's 200
    assert job.to_dict()["verdicts"] == {"pass": 8}
    assert io.calls[-1] == "warm"
    pb_lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    assert len(list((pb_lib / "Calibration" / "DARK").rglob("*.fits"))) == 6
    hist = (cq.qa_dir(cfg) / "capture_jobs.jsonl").read_text().splitlines()
    assert json.loads(hist[-1])["state"] == "complete"


def test_stops_when_roof_opens(tmp_path):
    cfg = _pb_cfg(tmp_path)
    io = FakeIO(cfg)

    def opener(fio):
        if fio.seq is not None and len(fio.to_land) < 4:
            fio.roof_closed = False
    io.on_camera = opener
    ok, _b, job = _run(_start_and_wait(cfg, io, exposures=[300.0], count=6))
    assert job.state == "aborted" and "roof opened" in job.detail
    assert "stop" in io.calls and io.calls[-1] == "warm"


def test_stops_on_temperature_drift(tmp_path):
    cfg = _pb_cfg(tmp_path)
    io = FakeIO(cfg, temps=[0.0, 0.0, 0.0, 0.0, 2.5])
    ok, _b, job = _run(_start_and_wait(cfg, io, exposures=[300.0], count=20))
    assert job.state == "aborted" and "outside" in job.detail
    assert "stop" in io.calls


@pytest.mark.parametrize("new_state,pre_h,stops", [
    ("ARMED", 3.0, True),      # dispatch hours away: stop our sequence
    ("ARMED", 0.05, False),    # dispatch imminent: hands off
    ("ARMED", None, False),    # no armer status: hands off
    ("RUNNING", 3.0, False),   # the night owns NINA: hands off
])
def test_armer_taking_over(tmp_path, new_state, pre_h, stops):
    from datetime import timedelta
    cfg = _pb_cfg(tmp_path)
    io = FakeIO(cfg)
    states = {"s": "DISARMED"}

    def arm(fio):
        if fio.seq is not None:
            states["s"] = new_state
    io.on_camera = arm
    status = None
    if pre_h is not None:
        pre = (datetime.utcnow() + timedelta(hours=pre_h)).isoformat() + "Z"
        status = lambda: {"state": states["s"], "preconfig_utc": pre}  # noqa: E731

    async def go():
        ok, _b = await cc.start_job(cfg, "piggyback", armer_state_fn=lambda: states["s"],
                                    io=io, poll_s=0.01, daytime=False,
                                    exposures=[300.0], count=5, armer_status_fn=status)
        await asyncio.wait_for(cc._jobs["piggyback"].task, 20)
        return cc._jobs["piggyback"]
    job = _run(go())
    assert job.state == "aborted" and f"armer went {new_state}" in job.detail
    assert ("stop" in io.calls) is stops
    assert "warm" not in io.calls   # arm() owns the cooler now


def test_cancel_and_budget(tmp_path):
    cfg = _pb_cfg(tmp_path)
    io = FakeIO(cfg)

    async def go():
        ok, _b = await cc.start_job(cfg, "piggyback", armer_state_fn=lambda: "DISARMED",
                                    io=io, poll_s=0.01, daytime=False,
                                    exposures=[300.0], count=50)
        assert cc.busy("piggyback")
        await asyncio.sleep(0.05)
        await cc.cancel("piggyback")
        await asyncio.wait_for(cc._jobs["piggyback"].task, 20)
        return cc._jobs["piggyback"]
    job = _run(go())
    assert job.state == "cancelled" and not cc.busy("piggyback")
    assert io.calls[-1] == "warm"

    io2 = FakeIO(cfg)
    ok, body, job = _run(_start_and_wait(cfg, io2, exposures=[300.0], count=2,
                                         budget=0.0))
    assert not ok and "nothing to capture" in body["refusals"][0]


def test_cooling_timeout_never_dispatches(tmp_path, monkeypatch):
    cfg = _pb_cfg(tmp_path)
    monkeypatch.setattr(cc, "COOL_TIMEOUT_MIN", 0.0)
    io = FakeIO(cfg, temps=[15.0])
    ok, _b, job = _run(_start_and_wait(cfg, io, exposures=[300.0], count=2))
    assert job.state == "aborted" and "did not reach" in job.detail
    assert "dispatch" not in io.calls and io.calls[-1] == "warm"


def test_light_in_frames_stops_job(tmp_path):
    cfg = _pb_cfg(tmp_path)
    io = FakeIO(cfg, frame_kw={"stars": 60, "shape": (256, 384)})
    ok, _b, job = _run(_start_and_wait(cfg, io, exposures=[300.0], count=10))
    assert job.state == "aborted" and "light in the frames" in job.detail
    pb_lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    assert list((pb_lib / "Calibration" / "_quarantine").rglob("*.fits"))
    assert not (pb_lib / "Calibration" / "DARK").exists()


def test_one_job_per_rig_and_dispatch_raw_refused(tmp_path):
    cfg = _cfg(tmp_path)

    async def go():
        io = FakeIO(cfg, rig="rc16", temps=[20.0])   # stays cooling
        ok, _b = await cc.start_job(cfg, "rc16", armer_state_fn=lambda: "DISARMED",
                                    io=io, poll_s=0.01, daytime=False,
                                    exposures=[300.0], count=2)
        assert ok and cc.busy("rc16")
        ok2, body2 = await cc.start_job(cfg, "rc16", armer_state_fn=lambda: "DISARMED",
                                        io=FakeIO(cfg, rig="rc16"), poll_s=0.01,
                                        daytime=False, exposures=[300.0], count=2)
        from photonscript.scheduler.armer import Armer
        armer = Armer(cfg)
        raw = await armer.dispatch_raw({"x": 1}, "auto dusk flats")
        await cc.cancel("rc16")
        await asyncio.wait_for(cc._jobs["rc16"].task, 20)
        return ok2, body2, raw, armer.detail
    ok2, body2, raw, detail = _run(go())
    assert not ok2 and any("already running" in r for r in body2["refusals"])
    assert raw is False and "calibration capture job" in detail


def test_daytime_probe_goes_first(tmp_path):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    lib_set(lib, "DARK", D1, 5, prefix="n", level=261.0, date_obs=NIGHT)
    cq.backfill(cfg, "piggyback", dry_run=True)
    io = FakeIO(cfg, temps=[20.0])

    async def go():
        ok, body = await cc.start_job(cfg, "piggyback", armer_state_fn=lambda: "DISARMED",
                                      io=io, poll_s=0.01, daytime=True,
                                      exposures=[300.0, 400.0], count=4)
        await cc.cancel("piggyback")
        await asyncio.wait_for(cc._jobs["piggyback"].task, 20)
        return ok, body
    ok, body = _run(go())
    assert ok and body["probe"] == [120.0, 3]
    assert body["darks"][0] == [120.0, 3]


def test_autofill_off_by_default_and_never_at_night(tmp_path, monkeypatch):
    cfg = _pb_cfg(tmp_path)
    assert PhotonScriptConfig(_env_file=None).calibration_autofill is False
    res = _run(cc.autofill_tick(cfg, lambda: "DISARMED"))
    assert res == {"skipped": "calibration_autofill is off"}
    on = _pb_cfg(tmp_path, calibration_autofill=True)
    monkeypatch.setattr(cc, "sun_alt_now", lambda c, now=None: -20.0)
    res = _run(cc.autofill_tick(on, lambda: "DISARMED"))
    assert res["skipped"].startswith("sun not up")
    monkeypatch.setattr(cc, "sun_alt_now", lambda c, now=None: 40.0)
    res = _run(cc.autofill_tick(on, lambda: "ARMED"))
    assert res["skipped"] == "armer ARMED"


def test_config_defaults_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.calibration_qa_mode == "quarantine"
    assert c.calibration_temp_tol_c == 1.0
    assert c.calibration_capture_budget_min == 240.0
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    for env, typ in (("PS_CALIBRATION_QA_MODE", "str"),
                     ("PS_CALIBRATION_TEMP_TOL_C", "float"),
                     ("PS_CALIBRATION_CAPTURE_BUDGET_MIN", "float"),
                     ("PS_CALIBRATION_AUTOFILL", "bool")):
        assert by_env[env][4] == typ and hasattr(c, by_env[env][0])


def test_real_rig_io_targets_its_own_nina(tmp_path):
    from photonscript.shared import rigs
    cfg = _pb_cfg(tmp_path, piggyback_nina_base_url="http://localhost:1889/v2/api",
                  nina_base_url="http://localhost:1888/v2/api")
    assert _REAL_RIGIO(cfg, "piggyback").base == "http://localhost:1889/v2/api"
    assert _REAL_RIGIO(cfg, "rc16").base == "http://localhost:1888/v2/api"
    assert rigs.RIG_DEVICES["piggyback"] == ("camera", "focuser")


def test_legacy_capture_endpoint_uses_the_guarded_job(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _pb_cfg(tmp_path)
    monkeypatch.setattr(app, "get_config", lambda: cfg)

    class _A:
        state = "ARMED"
    monkeypatch.setattr(app, "get_armer", lambda: _A())
    monkeypatch.setattr(cc, "RigIO", lambda c, r: FakeIO(c, rig=r))
    res = _run(app.api_calibration_capture({"rig": "piggyback"}))
    assert res.status_code == 409
    body = json.loads(res.body)
    assert "armer is ARMED" in body["detail"]


def test_generate_darks_json_gating_and_no_bias():
    from photonscript.scheduler.calibration import generate_darks_json
    cfg = PhotonScriptConfig(_env_file=None)
    plain, _ = generate_darks_json(cfg, [(300.0, 2)])
    assert "LoopWhileUnsafe" not in plain and "BIAS x50" in plain
    gated, _ = generate_darks_json(cfg, [(300.0, 2)], 0, safety_gated=True,
                                   warm_minutes=0.0)
    assert "LoopWhileUnsafe" in gated and "BIAS" not in json.dumps(
        [b for b in _blocks(json.loads(gated))])


def test_too_few_day_darks_leave_daytime_untested(tmp_path):
    cfg = _pb_cfg(tmp_path)
    lib = runs.library_root(cq.rig_view(cfg, "piggyback"))
    lib_set(lib, "DARK", D1, 5, prefix="n", level=261.0, date_obs=NIGHT)
    lib_set(lib, "DARK", D4, 1, prefix="d", level=275.0, date_obs=DAY)
    cq.backfill(cfg, "piggyback")
    assert cq.daytime_state(cfg, "piggyback") == {"status": "untested"}
    ev = cq.load_store(cfg, "piggyback")["daytime"]["evidence"]
    assert ev and ev[0]["leak"] is None and "only 1 day dark" in ev[0]["detail"]
