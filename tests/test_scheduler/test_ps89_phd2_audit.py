"""PS-89 PHD2 settings audit: the desired-state file, evaluation on the
09-18 / 09-25 / 09-26 guide-log fixtures, the registry profile store on a
fake winreg, collect + apply on the fake PHD2, the arm hook (one push only
on a FAIL, never blocks), the ConfigurationChange re-audit, routes, the
System / runs page and preflight."""
import asyncio
import json
import shutil
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import phd2_audit as pa
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler import phd2_profile_store as ps
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.telescope_agent import phd2_ops
from photonscript.telescope_agent.phd2_client import PHD2Client
from tests.fakes.fake_phd2 import FakePHD2

FIX = Path(__file__).parent / "fixtures" / "phd2"
N26 = "PHD2_GuideLog_2026-09-26_120453.txt"
N25 = "PHD2_GuideLog_2026-09-25_192414.txt"
N18 = "PHD2_GuideLog_2026-09-18_204456.txt"


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
        kw.setdefault("phd2_logs_dir", str(tmp_path / "logs"))
        kw.setdefault("phd2_darks_dir", str(tmp_path / "darks"))
    return PhotonScriptConfig(_env_file=None, **kw)


def _secs(name):
    return pl.parse_guide_log((FIX / name).read_text(encoding="utf-8"), name)


def _eval(observed, cfg=None):
    cfg = cfg or _cfg()
    obs = {s: {} for s in pa.SOURCES}
    obs.update(observed)
    res = pa.evaluate(pa.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}, res


@pytest.fixture(autouse=True)
def _free_ops():
    phd2_ops._reset_for_tests()
    yield
    phd2_ops._reset_for_tests()


# --- the desired-state file ---------------------------------------------------

def test_desired_file_lints_clean_and_every_row_explains_itself():
    d = pa.load_desired(_cfg())
    assert not d.get("error") and not d.get("lint"), d.get("lint")
    rows = d["check"]
    ids = {r["id"] for r in rows}
    for want in ("bit_depth", "saturation_adu", "exposure_ms", "search_region_px",
                 "mass_change", "min_hfd_px", "multi_star", "ra_algorithm",
                 "ra_min_move", "dec_algorithm", "backlash_comp",
                 "calibration_step_ms", "auto_restore_cal", "dec_compensation",
                 "reverse_dec_after_flip", "focal_length_mm", "guide_speed",
                 "dark_library", "defect_map", "nina_settle_px",
                 "ascom_direct_guide", "ascom_pointing_state", "thesky_scripting"):
        assert want in ids, want
    for r in rows:
        assert len(r["why"]) > 20 and r["fix"], r["id"]
        # mount driver / TheSky / NINA rows are never changed
        if set(r["sources"]) & {"ascom", "thesky", "nina"}:
            assert r["apply"] == "manual", r["id"]
    raw = pa.DEFAULT_FILE.read_bytes()
    assert all(b < 128 for b in raw)


def test_lint_catches_a_bad_row():
    bad = {"check": [{"id": "x", "group": "G", "label": "L", "sources": ["nope"],
                      "severity": "bad", "apply": "api", "why": "", "fix": "f"},
                     {"id": "x", "group": "G", "label": "L", "sources": ["thesky"],
                      "equals": True, "severity": "fail", "apply": "api",
                      "why": "w " + chr(0x2014) + " dash", "fix": "f"}]}
    p = " | ".join(pa.lint_desired(bad))
    for frag in ("bad sources", "severity", "no why", "duplicate id",
                 "no desired value", "no API setter", "report only", "plain ASCII"):
        assert frag in p, frag


# --- evaluation on the guide-log fixtures ----------------------------------------

def test_0918_fails_on_the_600_mm_focal_length():
    log = pa.log_observed(_secs(N18), _cfg())
    rows, res = _eval({"log": log})
    fl = rows["focal_length_mm"]
    assert fl["status"] == pa.FAIL and fl["current"] == "600" and fl["source"] == "log"
    assert "mm" in fl["desired"] and fl["target"] and 3100 < fl["target"] < 3400
    assert rows["defect_map"]["status"] == pa.FAIL       # "no defect map"
    assert res["counts"]["fail"] >= 4


def test_0926_fails_the_settings_the_diagnosis_found():
    secs = _secs(N26)
    log = pa.log_observed(secs, _cfg())
    assert log["bit_depth"] == 8 and "inferred" in log["bit_depth_note"]
    rows, _ = _eval({"log": log})
    assert rows["bit_depth"]["status"] == pa.FAIL
    sr = rows["search_region_px"]
    assert sr["status"] == pa.FAIL and sr["current"] == "15"
    assert "dither" in sr["note"] and "2 x dither" in sr["desired"]
    assert rows["mass_change"]["status"] == pa.FAIL and rows["mass_change"]["current"] == "50"
    assert rows["dec_algorithm"]["status"] == pa.FAIL
    assert rows["dec_algorithm"]["current"] == "Lowpass2"
    # the newest session (21:54) had the defect map but no dark
    assert rows["have_dark"]["status"] == pa.WARN
    # the first guiding session (20:18) ran with no defect map
    first = [s for s in secs if s["kind"] == "calibration"][:1] + \
        [s for s in secs if s["kind"] == "guiding"][:1]
    rows1, _ = _eval({"log": pa.log_observed(first, _cfg())})
    assert rows1["defect_map"]["status"] == pa.FAIL
    assert rows1["calibration_step_ms"]["current"] == "250"


def test_ga_min_move_and_calibration_step_targets():
    secs = _secs(N25)
    cfg = _cfg()
    log = pa.log_observed(secs, cfg)
    from photonscript.scheduler import phd2_analysis as an
    ga = pa.ga_observed(an.ga_results(secs, cfg))
    assert ga["ra_min_move_rec"] == 1.5 and ga["dec_min_move_rec"] == 1.5
    rows, _ = _eval({"log": log, "ga": ga,
                     "calibration": {"grade": "PASS", "recommended_step_ms": 50}})
    mm = rows["ra_min_move"]
    assert mm["status"] == pa.WARN and mm["target"] == 1.5 and mm["applicable"]
    assert rows["ra_min_move"]["current"] == "0.76"
    cs = rows["calibration_step_ms"]
    assert cs["status"] == pa.WARN and cs["target"] == 50
    ok, _ = _eval({"log": dict(log, ra_min_move=1.4), "ga": ga})
    assert ok["ra_min_move"]["status"] == pa.PASS


@pytest.mark.parametrize("owner,algo,status", [
    ("protrack", "Hysteresis", pa.PASS), ("protrack", "Predictive PEC", pa.FAIL),
    ("phd2_ppec", "Predictive PEC", pa.PASS), ("phd2_ppec", "Lowpass2", pa.FAIL),
    ("none", "Predictive PEC", pa.INFO)])
def test_pe_owner(owner, algo, status):
    rows, _ = _eval({"api": {"ra_algorithm": algo}}, _cfg(pe_owner=owner))
    assert rows["ra_algorithm"]["status"] == status


def test_arcsec_rows_convert_at_the_guide_scale_and_calibration_record():
    rows, res = _eval({"api": {"binning": 2},
                       "nina": {"nina_settle_px": 1.5, "nina_settle_timeout_s": 40},
                       "calibration": {"grade": "FAIL", "poor": True,
                                       "reasons": ["ortho 15.1 deg"],
                                       "flip": {"West": {"ok": False, "detail": "ran away"}}}})
    s = res["scale_arcsec_px"]
    assert 0.24 < s < 0.27
    st = rows["nina_settle_px"]
    assert st["status"] == pa.WARN and "px at" in st["desired"]
    assert rows["nina_settle_timeout_s"]["status"] == pa.WARN
    assert rows["calibration_record"]["status"] == pa.FAIL
    assert "15.1" in rows["calibration_record"]["note"]
    assert rows["reverse_dec_after_flip"]["status"] == pa.FAIL
    ok, _ = _eval({"api": {"binning": 2}, "nina": {"nina_settle_px": 3.0},
                   "calibration": {"grade": "PASS", "age_days": 2}})
    assert ok["nina_settle_px"]["status"] == pa.PASS
    assert ok["calibration_record"]["status"] == pa.PASS
    assert ok["reverse_dec_after_flip"]["status"] == pa.INFO


def test_guide_speed_against_the_mount_and_a_zero_rate():
    nina = {"guide_speed": {"ra": 7.52, "dec": 7.52, "source": "nina", "zero": False}}
    rows, _ = _eval({"log": {"guide_speed": 7.5}, "nina": nina})
    assert rows["guide_speed"]["status"] == pa.PASS
    rows, _ = _eval({"log": {"guide_speed": 15.0}, "nina": nina})
    assert rows["guide_speed"]["status"] == pa.FAIL
    rows, _ = _eval({"log": {"guide_speed": 7.5},
                     "nina": {"guide_speed": {"ra": None, "source": "config", "zero": True}}})
    assert rows["guide_speed"]["status"] == pa.FAIL and "0" in rows["guide_speed"]["note"]


def test_ascom_and_unverified_profile_rows_are_unknown_not_fail():
    rows, res = _eval({"profile_candidates": {
        "saturation_adu": {"value": 255, "verified": False,
                           "location": "camera/SaturationADU"},
        "bit_depth": {"value": None, "verified": False, "location": None}}})
    sat = rows["saturation_adu"]
    assert sat["status"] == pa.UNKNOWN and "unverified" in sat["note"]
    assert "camera/SaturationADU = 255" in sat["note"]
    assert "not known yet" in rows["bit_depth"]["note"]
    for rid in ("ascom_direct_guide", "ascom_pointing_state"):
        assert rows[rid]["status"] == pa.UNKNOWN and "by hand" in rows[rid]["note"]
    assert not any(r["status"] == pa.FAIL for r in rows.values()
                   if r["id"] in ("saturation_adu", "ascom_direct_guide"))


def _dark_lib(path, exposures_ms):
    from astropy.io import fits
    hdus = [fits.PrimaryHDU()]
    for e in exposures_ms:
        h = fits.ImageHDU(np.zeros((4, 4), dtype=np.uint16))
        h.header["EXPOSURE"] = e
        hdus.append(h)
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.HDUList(hdus).writeto(path, overwrite=True)


def test_dark_library_file_checks(tmp_path):
    cfg = _cfg(tmp_path)
    obs, src = pa._collect_files(cfg, 1)
    assert obs["dir_missing"] and not src["ok"]
    rows, _ = _eval({"file": obs}, cfg)
    assert rows["dark_library"]["status"] == pa.UNKNOWN
    d = Path(cfg.phd2_darks_dir)
    _dark_lib(d / "PHD2_dark_lib_1.fit", [1000, 2000, 3000])
    obs, _ = pa._collect_files(cfg, 1)
    assert obs["dark_library"]["exposures_ms"] == [1000, 2000, 3000]
    assert obs["defect_map"] is False
    rows, _ = _eval({"file": obs}, cfg)
    assert rows["dark_library"]["status"] == pa.FAIL           # no 4 s dark
    assert "1 to 4 s" in rows["dark_library"]["note"]
    assert rows["defect_map"]["status"] == pa.FAIL
    _dark_lib(d / "PHD2_dark_lib_1.fit", [1000, 2000, 3000, 4000])
    (d / "PHD2_defect_map_1.fit").write_bytes(b"x")
    obs, _ = pa._collect_files(cfg, 1)
    rows, _ = _eval({"file": obs, "log": {"exposure_ms": 2000}}, cfg)
    assert rows["dark_library"]["status"] == pa.PASS
    assert rows["defect_map"]["status"] == pa.PASS
    rows, _ = _eval({"file": obs, "log": {"exposure_ms": 2500}}, cfg)
    assert rows["dark_library"]["status"] == pa.FAIL           # no 2.5 s dark


# --- the registry profile store (fake winreg) ----------------------------------

def _node(values=None, keys=None):
    return {"values": dict(values or {}), "keys": dict(keys or {})}


class _Key:
    def __init__(self, node):
        self.node = node

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeWinreg:
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    REG_SZ, REG_DWORD = 1, 4
    KEY_READ, KEY_SET_VALUE = 0x20019, 0x2

    def __init__(self, hkcu):
        self.roots = {"HKCU": hkcu, "HKLM": _node()}
        self.sets = []

    def OpenKey(self, key, sub, reserved=0, access=0):
        node = self.roots[key] if isinstance(key, str) else key.node
        for part in [p for p in sub.split("\\") if p]:
            if part not in node["keys"]:
                raise FileNotFoundError(part)
            node = node["keys"][part]
        return _Key(node)

    def EnumValue(self, k, i):
        items = list(k.node["values"].items())
        if i >= len(items):
            raise OSError("no more")
        name, (v, t) = items[i]
        return name, v, t

    def EnumKey(self, k, i):
        names = list(k.node["keys"])
        if i >= len(names):
            raise OSError("no more")
        return names[i]

    def QueryValueEx(self, k, name):
        if name not in k.node["values"]:
            raise FileNotFoundError(name)
        return k.node["values"][name]

    def SetValueEx(self, k, name, reserved, typ, v):
        self.sets.append((name, v, typ))
        k.node["values"][name] = (v, typ)


def _registry():
    S, D = FakeWinreg.REG_SZ, FakeWinreg.REG_DWORD
    prof = _node({"name": ("Primary RC Profile (Guider)", S),
                  "ExposureDurationMs": (2000, D), "AutoLoadCalibration": (1, D)}, {
        "camera": _node({"pixelsize": ("2", S), "gain": (100, D),
                         "SaturationADU": (255, D), "SaturationByADU": (1, D)}),
        "guider": _node({"StarMinHFD": ("1.5", S)}, {
            "onestar": _node({"SearchRegion": (15, D),
                              "MassChangeThresholdEnabled": (1, D),
                              "MassChangeThreshold": ("50", S)})})})
    other = _node({"name": ("Piggy Back - 600mm", S)})
    root = _node({"currentProfile": (1, D)}, {"profile": _node(keys={"1": prof, "2": other})})
    return FakeWinreg(_node(keys={"Software": _node(keys={"StarkLabs": _node(
        keys={"PHDGuidingV2": root})})}))


def test_profile_store_reads_flat_and_resolves_the_profile(monkeypatch):
    reg = _registry()
    monkeypatch.setattr(ps, "_winreg", lambda: reg)
    lp = ps.list_profiles()
    assert lp["available"] and {p["id"] for p in lp["profiles"]} == {"1", "2"}
    assert ps.resolve_id(None, "Piggy Back - 600mm") == "2"
    assert ps.resolve_id(None, None) == "1"                      # current
    r = ps.read(None, "Primary RC Profile (Guider)")
    assert r["profile_id"] == "1"
    assert r["raw"]["guider/onestar/SearchRegion"] == 15
    v = r["values"]["search_region_px"]
    assert v["value"] == 15 and v["verified"] is False         # nothing verified yet
    vals, cands = pa._profile_observed(r)
    assert vals == {} and cands["saturation_adu"]["value"] == 255
    monkeypatch.setattr(ps, "_winreg", lambda: None)
    assert ps.read(1)["available"] is False


def test_profile_write_refusals_and_a_verified_round_trip(tmp_path, monkeypatch):
    reg = _registry()
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(ps, "_winreg", lambda: reg)

    def _export(key, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("Windows Registry Editor Version 5.00\n" + key, encoding="utf-8")
        return True, "ok"
    monkeypatch.setattr(ps, "_reg_export", _export)
    b = ps.backup(cfg, "1")
    assert b["ok"] and Path(b["reg"]).exists() and Path(b["json"]).exists()
    assert "guider/onestar/SearchRegion" in json.loads(Path(b["json"]).read_text())
    monkeypatch.setattr(ps, "phd2_running", lambda: True)
    assert "running" in ps.write(cfg, "1", {"search_region_px": 35}, backup_path=b["reg"])["note"]
    monkeypatch.setattr(ps, "phd2_running", lambda: False)
    assert "no backup" in ps.write(cfg, "1", {"search_region_px": 35}, backup_path=None)["note"]
    r = ps.write(cfg, "1", {"search_region_px": 35}, backup_path=b["reg"])
    assert not r["ok"] and "unverified" in r["refused"]["search_region_px"]
    assert reg.sets == []
    monkeypatch.setitem(ps.KEYS, "search_region_px", ("guider/onestar", "SearchRegion", True))
    monkeypatch.setitem(ps.KEYS, "min_hfd_px", ("guider", "StarMinHFD", True))
    r = ps.write(cfg, "1", {"search_region_px": 35, "min_hfd_px": 1.5}, backup_path=b["reg"])
    assert r["ok"] and r["written"] == {"search_region_px": 35, "min_hfd_px": "1.5"}
    assert (35, FakeWinreg.REG_DWORD) == reg.roots["HKCU"]["keys"]["Software"]["keys"][
        "StarkLabs"]["keys"]["PHDGuidingV2"]["keys"]["profile"]["keys"]["1"]["keys"][
        "guider"]["keys"]["onestar"]["values"]["SearchRegion"]
    after = ps.read("1")
    assert after["values"]["search_region_px"]["value"] == 35


def test_apply_profile_rows_are_gated(tmp_path, monkeypatch):
    reg = _registry()
    monkeypatch.setattr(ps, "_winreg", lambda: reg)
    monkeypatch.setattr(ps, "phd2_running", lambda: False)
    monkeypatch.setattr(ps, "_reg_export", lambda key, path: (
        path.parent.mkdir(parents=True, exist_ok=True), path.write_text("x"), (True, ""))[-1])
    audit = {"profile_id": "1", "rows": [
        {"id": "search_region_px", "apply": "profile", "status": pa.FAIL,
         "target": 35, "current": "15"},
        {"id": "mass_change", "apply": "profile", "status": pa.FAIL,
         "target": "off", "current": "50"},
        {"id": "dec_algorithm", "apply": "manual", "status": pa.FAIL,
         "target": "Resist Switch", "current": "Lowpass2"}]}
    ids = ["search_region_px", "mass_change", "dec_algorithm"]

    async def go(cfg, state, dry=False):
        r = await pa.apply(cfg, ids, dry_run=dry, armer_state=state, audit=audit)
        return {x["id"]: x for x in r["results"]}
    off = asyncio.run(go(_cfg(tmp_path), "DISARMED"))
    assert "phd2_audit_autofix" in off["search_region_px"]["note"]
    assert "report only" in off["dec_algorithm"]["note"]
    cfg = _cfg(tmp_path, phd2_audit_autofix=True)
    assert "armer is RUNNING" in asyncio.run(go(cfg, "RUNNING"))["search_region_px"]["note"]
    assert "unverified" in asyncio.run(go(cfg, "DISARMED"))["mass_change"]["note"]
    for k, loc in (("search_region_px", ("guider/onestar", "SearchRegion", True)),
                   ("mass_change_enabled", ("guider/onestar", "MassChangeThresholdEnabled", True)),
                   ("mass_change_pct", ("guider/onestar", "MassChangeThreshold", True))):
        monkeypatch.setitem(ps.KEYS, k, loc)
    dry = asyncio.run(go(cfg, "DISARMED", dry=True))
    assert dry["mass_change"]["ok"] and dry["mass_change"]["to"] == {"mass_change_enabled": False}
    assert reg.sets == []
    monkeypatch.setattr(ps, "phd2_running", lambda: True)
    assert "PHD2 is running" in asyncio.run(go(cfg, "COMPLETE"))["search_region_px"]["note"]
    monkeypatch.setattr(ps, "phd2_running", lambda: False)
    res = asyncio.run(go(cfg, "COMPLETE"))
    assert res["search_region_px"]["ok"] and res["mass_change"]["ok"]
    assert ("MassChangeThresholdEnabled", 0, FakeWinreg.REG_DWORD) in reg.sets
    ch = store.read_jsonl(pa.changes_path(cfg))
    assert {c["id"] for c in ch} == {"search_region_px", "mass_change"}
    assert all(c["backup"] for c in ch)


# --- collect + apply on the fake PHD2 -------------------------------------------

class FakeNina:
    def __init__(self, settle=1.5, timeout=40, dither=5, rate=7.52):
        self.p = {"Name": "RC16", "GuiderSettings": {
            "SettlePixels": settle, "SettleTime": 10, "SettleTimeout": timeout,
            "DitherPixels": dither}}
        self.rate = rate

    async def get_profile(self):
        return self.p

    async def get_mount_info(self):
        return {"GuideRateRightAscensionArcsecPerSec": self.rate,
                "GuideRateDeclinationArcsecPerSec": self.rate}

    async def close(self):
        pass


def _hermetic(monkeypatch):
    monkeypatch.setattr(ps, "_winreg", lambda: None)
    monkeypatch.setattr(pa, "_collect_thesky",
                        lambda config: ({"thesky_scripting": False},
                                        {"ok": True, "note": "refused"}))


async def test_collect_on_the_fake_phd2_and_nina(tmp_path, monkeypatch):
    _hermetic(monkeypatch)
    cfg = _cfg(tmp_path)
    (tmp_path / "logs").mkdir()
    shutil.copy(FIX / N25, tmp_path / "logs" / N25)
    fake = FakePHD2(tmp_path / "phd2", app_state="Looping")
    port = await fake.start()
    client = PHD2Client("127.0.0.1", port, config=cfg)
    assert await client.connect()
    try:
        a = await pa.run_audit(cfg, "test", client=client, nina=FakeNina(), raw=True)
    finally:
        await client.disconnect()
        await fake.close()
    api = a["observed"]["api"]
    assert api["app_state"] == "Looping" and api["profile_id"] == 1
    assert api["camera"] == "GP678C" and api["search_region_px"] == 15
    assert api["dec_algorithm"] == "Lowpass2" and api["ra_min_move"] == 0.76
    assert 2900 < api["focal_length_mm"] < 3300 and api["exposure_durations"]
    rows = {r["id"]: r for r in a["rows"]}
    assert rows["search_region_px"]["source"] == "api"
    assert rows["search_region_px"]["status"] == pa.FAIL
    assert rows["ra_min_move"]["target"] == 1.5                     # 09-25 GA
    assert rows["nina_settle_px"]["status"] == pa.WARN
    assert rows["nina_settle_timeout_s"]["status"] == pa.WARN
    assert rows["guide_speed"]["status"] == pa.PASS
    assert rows["thesky_scripting"]["status"] == pa.WARN
    assert a["sources"]["profile"]["ok"] is False
    assert a["sources"]["api"]["ok"] and a["sources"]["nina"]["ok"]
    assert pa.load_latest(cfg)["t_utc"] == a["t_utc"]
    assert pa.load_night(cfg, a["night"])["fail_ids"] == a["fail_ids"]
    assert rows["exposure_ms"]["status"] == pa.PASS or rows["exposure_ms"]["current"]


async def test_collect_never_raises_without_phd2_or_nina(tmp_path, monkeypatch):
    _hermetic(monkeypatch)
    cfg = _cfg(tmp_path, phd2_port=1, nina_base_url="http://127.0.0.1:1/v2/api")
    a = await pa.run_audit(cfg, "test")
    assert a["sources"]["api"]["ok"] is False
    assert a["sources"]["nina"]["ok"] is False
    assert a["counts"]["unknown"] > 10 and a["rows"]


async def _apply_on_fake(tmp_path, monkeypatch, state, ids, durations=None, hold=None):
    _hermetic(monkeypatch)
    cfg = _cfg(tmp_path)
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    shutil.copy(FIX / N25, tmp_path / "logs" / N25)
    fake = FakePHD2(tmp_path / "phd2", app_state=state, durations=durations,
                    dec_guide_mode="Off")
    fake.guide_exposure_ms = 1000
    port = await fake.start()
    client = PHD2Client("127.0.0.1", port, config=cfg)
    await client.connect()
    try:
        audit = await pa.run_audit(cfg, "test", client=client, nina=FakeNina())
        if hold:
            async with phd2_ops.hold(hold):
                r = await pa.apply(cfg, ids, dry_run=False, client=client, audit=audit)
        else:
            r = await pa.apply(cfg, ids, dry_run=False, client=client, audit=audit)
    finally:
        await client.disconnect()
        await fake.close()
    return cfg, fake, {x["id"]: x for x in r["results"]}


async def test_apply_api_rows_while_looping_uses_listed_durations(tmp_path, monkeypatch):
    cfg, fake, res = await _apply_on_fake(
        tmp_path, monkeypatch, "Looping",
        ["exposure_ms", "ra_min_move", "dec_guide_mode", "dec_algorithm", "camera"],
        durations=[1000, 2500, 3500, 6000])
    assert res["exposure_ms"]["ok"] and res["exposure_ms"]["to"] == 2500
    sets = [r["params"][0] for r in fake.requests if r["method"] == "set_exposure"]
    assert sets == [2500] and all(s in fake.durations for s in sets)
    assert res["ra_min_move"]["ok"] and fake.algo["ra"]["MinMove"] == 1.5
    assert res["dec_guide_mode"]["ok"] and fake.dec_guide_mode == "Auto"
    assert "report only" in res["dec_algorithm"]["note"]
    assert res["camera"]["ok"] and "already" in res["camera"]["note"]
    ch = store.read_jsonl(pa.changes_path(cfg))
    assert {c["id"] for c in ch} == {"exposure_ms", "ra_min_move", "dec_guide_mode"}
    assert phd2_ops.owner() is None


async def test_apply_refuses_while_guiding_or_when_phd2_is_held(tmp_path, monkeypatch):
    _cfg_, fake, res = await _apply_on_fake(tmp_path, monkeypatch, "Guiding",
                                            ["exposure_ms", "dec_guide_mode"])
    assert not res["exposure_ms"]["ok"] and "Guiding" in res["exposure_ms"]["note"]
    assert not [r for r in fake.requests if r["method"].startswith("set_")]
    _c, fake2, res2 = await _apply_on_fake(tmp_path / "b", monkeypatch, "Stopped",
                                           ["exposure_ms"], hold="selftest")
    assert "busy (selftest)" in res2["exposure_ms"]["note"]
    assert not [r for r in fake2.requests if r["method"].startswith("set_")]


# --- arm hook, re-audit, routes, pages ---------------------------------------------

def _audit_with(fail_ids):
    rows = [{"id": i, "label": f"Label {i}", "status": pa.FAIL, "current": "x",
             "desired": "y"} for i in fail_ids]
    return {"night": "2026-10-01", "rows": rows, "counts": {"fail": len(rows)},
            "fail_ids": list(fail_ids)}


def _armer(tmp_path, monkeypatch, notes, **kw):
    import photonscript.scheduler.armer as armer_mod
    import photonscript.scheduler.night_plan as night_plan
    from photonscript.scheduler.armer import Armer

    async def _notify(cfg, msg, **k):
        notes.append(("armer", msg))
    monkeypatch.setattr(armer_mod, "notify", _notify)
    monkeypatch.setattr(night_plan, "build_night_plan", lambda config: {
        "night_of": "2026-10-01", "preconfig_utc": "2026-10-02T01:00:00Z",
        "targets": ["M31"], "dark_hours": 8.0})
    a = Armer(_cfg(tmp_path, **kw))

    async def _noop(*x, **k):
        return {}
    monkeypatch.setattr(a, "_run", _noop)
    monkeypatch.setattr(a, "connect_all_rigs", _noop)
    monkeypatch.setattr(a, "_cooler_dew_off_all", _noop)
    return a


async def test_arm_pushes_once_on_a_fail_and_never_blocks(tmp_path, monkeypatch):
    from photonscript.shared import pushover
    notes = []

    async def _push(cfg, msg, **k):
        notes.append(("audit", msg))
    monkeypatch.setattr(pushover, "notify", _push)
    gate = asyncio.Event()

    async def _run_audit(config, reason="manual", **kw):
        await gate.wait()
        return _audit_with(["search_region_px", "mass_change"])
    monkeypatch.setattr(pa, "run_audit", _run_audit)
    a = _armer(tmp_path, monkeypatch, notes)
    st = await asyncio.wait_for(a.arm("guided"), 5)       # returns with the audit pending
    assert st["state"] == "ARMED" and not a._audit_task.done()
    gate.set()
    await asyncio.wait_for(a._audit_task, 5)
    audit_pushes = [m for k, m in notes if k == "audit"]
    assert len(audit_pushes) == 1 and "2 FAIL" in audit_pushes[0]
    assert "Label search_region_px" in audit_pushes[0]
    await a.arm("guided")                                  # same night: no second push
    await asyncio.wait_for(a._audit_task, 5)
    assert len([m for k, m in notes if k == "audit"]) == 1
    # a re-audit with the same FAILs does not push; a new one does
    monkeypatch.setattr(pa, "run_audit", lambda config, reason="", **kw: _coro(
        _audit_with(["search_region_px", "dec_algorithm"])))
    r = await pa.on_config_change(a.config)
    assert r["new_fails"] == ["dec_algorithm"]
    assert len([m for k, m in notes if k == "audit"]) == 2


async def _coro(v):
    return v


async def test_arm_without_fails_or_unguided_does_not_push(tmp_path, monkeypatch):
    from photonscript.shared import pushover
    notes, calls = [], []

    async def _push(cfg, msg, **k):
        notes.append(msg)
    monkeypatch.setattr(pushover, "notify", _push)

    async def _run_audit(config, reason="manual", **kw):
        calls.append(reason)
        return _audit_with([])
    monkeypatch.setattr(pa, "run_audit", _run_audit)
    a = _armer(tmp_path, monkeypatch, [])
    await a.arm("guided")
    await asyncio.wait_for(a._audit_task, 5)
    assert calls == ["arm"] and notes == []
    b = _armer(tmp_path / "b", monkeypatch, [])
    await b.arm("encoders")
    assert b._audit_task is None
    c = _armer(tmp_path / "c", monkeypatch, [], phd2_audit_enabled=False)
    await c.arm("guided")
    assert c._audit_task is None

    async def _boom(config, reason="manual", **kw):
        raise RuntimeError("PHD2 exploded")
    monkeypatch.setattr(pa, "run_audit", _boom)
    d = _armer(tmp_path / "d", monkeypatch, [])
    st = await d.arm("guided")
    await asyncio.wait_for(d._audit_task, 5)               # swallowed, logged
    assert st["state"] == "ARMED"


async def test_reauditor_debounces_and_pushes_only_when_armed(tmp_path, monkeypatch):
    calls = []

    async def _occ(config, push=True, **kw):
        calls.append(push)
        return {}
    monkeypatch.setattr(pa, "on_config_change", _occ)
    armed = {"v": False}
    ra = pa.ReAuditor(_cfg(tmp_path), armed_fn=lambda: armed["v"], debounce_s=0.05)
    for _ in range(4):
        await ra.on_event({"Event": "ConfigurationChange"})
    await ra.on_event({"Event": "GuideStep"})
    await asyncio.sleep(0.2)
    assert calls == [False]
    armed["v"] = True
    await ra.on_event({"Event": "ConfigurationChange"})
    await asyncio.sleep(0.2)
    assert calls == [False, True]
    off = pa.ReAuditor(_cfg(tmp_path, phd2_audit_enabled=False), debounce_s=0.01)
    await off.on_event({"Event": "ConfigurationChange"})
    await asyncio.sleep(0.05)
    assert calls == [False, True]


async def test_routes_config_fields_preflight_and_pages(tmp_path, monkeypatch):
    from photonscript.scheduler import app, preflight
    from photonscript.scheduler.routers import phd2 as r
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    assert preflight._audit_check(cfg)["status"] == "warn"      # no audit yet
    ran = []

    async def _run_audit(config, reason="manual", raw=False, **kw):
        ran.append((reason, raw))
        a = dict(_audit_with(["focal_length_mm"]), t_utc="2026-10-02T01:00:00Z",
                 reason=reason, counts={"fail": 1, "warn": 0})
        pa.save(config, a)
        return a
    monkeypatch.setattr(pa, "run_audit", _run_audit)
    out = await r.api_phd2_audit(refresh=True)
    assert out["cached"] is False and out["phd2_ops"]["owner"] is None
    out = await r.api_phd2_audit()
    assert out["cached"] is True and ran == [("manual", False)]
    await r.api_phd2_audit(raw=True)
    assert ran[-1] == ("manual", True)
    pf = preflight._audit_check(cfg)
    assert pf["status"] == "warn" and "1 fail" in pf["detail"]
    s = pa.summary(cfg, "2026-10-01")
    assert s["counts"]["fail"] == 1 and "Label focal_length_mm" in s["fails"][0]

    class _Req:
        def __init__(self, body):
            self.body = body

        async def json(self):
            return self.body
    bad = await r.api_phd2_audit_apply(_Req({}))
    assert bad.status_code == 400
    monkeypatch.setattr(r, "_armer_state", lambda: "DISARMED")
    got = []

    async def _apply(config, ids, dry_run=True, armer_state=None, **kw):
        got.append((ids, dry_run, armer_state))
        return {"ok": True}
    monkeypatch.setattr(pa, "apply", _apply)
    await r.api_phd2_audit_apply(_Req({"ids": ["exposure_ms"]}))
    await r.api_phd2_audit_apply(_Req({"ids": ["exposure_ms"], "dry_run": False}))
    assert got == [(["exposure_ms"], True, "DISARMED"), (["exposure_ms"], False, "DISARMED")]
    by_env = {f[1]: f for f in app._CONFIG_FIELDS}
    for env in ("PS_PHD2_AUDIT_ENABLED", "PS_PHD2_AUDIT_AUTOFIX", "PS_PHD2_DESIRED_FILE",
                "PS_PHD2_DARK_MAX_AGE_DAYS", "PS_PHD2_DARKS_DIR", "PS_PE_OWNER"):
        assert by_env[env][3] == "PHD2" and hasattr(cfg, by_env[env][0])
    d = PhotonScriptConfig(_env_file=None)
    assert (d.phd2_audit_enabled, d.phd2_audit_autofix, d.phd2_desired_file,
            d.phd2_dark_max_age_days, d.phd2_darks_dir, d.pe_owner) == (
        True, False, "", 30.0, "", "protrack")
    tpl = Path(app.__file__).parent / "templates"
    sysh = (tpl / "system.html").read_text(encoding="utf-8")
    assert sysh.index('id="phd2Panel"') < sysh.index("<h2>Preflight Check</h2>")
    assert 'id="auditSec"' in (tpl / "guiding.html").read_text(encoding="utf-8")   # PS-103
    js = (Path(app.__file__).parent / "static" / "js" / "phd2_panels.js").read_text(encoding="utf-8")
    assert "/api/phd2/audit/apply" in js
    assert "PS-89 settings audit" in (tpl / "runs.html").read_text(encoding="utf-8")


def test_sources_are_ascii_without_em_dashes():
    root = Path(__file__).resolve().parents[2] / "photonscript"
    for rel in ("scheduler/phd2_audit.py", "scheduler/phd2_profile_store.py"):
        assert all(b < 128 for b in (root / rel).read_bytes()), rel
