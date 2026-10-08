"""PS-171: the TPoint numbers from TPoint's own files (read only).

TheSky64 10.5 build 14139 has no TPoint scripting object, so points, sky
RMS, terms, polar alignment and ProTrack come from the files under
TheSky's user folder. fixtures/tpoint/documented-format_* are written from
the documented TPOINT formats (input data: caption, ":" options, site line,
observations, END; fit report: numbered term lines, Sky RMS, Popn SD).
fixtures/tpoint/thesky64_* are the real files read from the scope PC with
GET /api/thesky/tpoint/file?rel= on 2026-10-08 (TheSky64 build 14139,
byte for byte, CRLF)."""
import asyncio
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from photonscript.scheduler import tpoint_files as tf
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "tpoint"


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("nina_logs_dir", str(tmp_path / "ninalogs"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


def _tree(tmp_path) -> Path:
    """A TheSky user folder shaped like the desktop copy: <root>/TPoint
    with the fixtures, an older data file, and Camera AutoSave frames."""
    root = tmp_path / "Software Bisque" / "TheSky Professional Edition 64"
    tp = root / "TPoint"
    tp.mkdir(parents=True)
    old = tp / "old_run.dat"
    old.write_text((FIX / "documented-format_pointing.dat").read_text(
        encoding="ascii").replace("01 15 45.00", "XX"), encoding="ascii")
    os.utime(old, (time.time() - 86400 * 30, time.time() - 86400 * 30))
    for f in FIX.glob("documented-format_*"):
        shutil.copy2(f, tp / f.name)
    auto = root / "Camera AutoSave" / "Imager" / "Automated Pointing Run 001"
    auto.mkdir(parents=True)
    (auto / "TPoint_0001.fit").write_bytes(b"SIMPLE  =" + b"\x00" * 100)
    (root / "Satellites").mkdir()
    return root


# ------------------------------------------------------------- parsers

def test_parse_data_counts_observations_and_reads_the_date():
    d = tf.parse_data((FIX / "documented-format_pointing.dat").read_text(encoding="ascii"))
    assert d["points"] == 5 and d["date"] == "2026-10-09"
    assert d["options"] == ["NODA", "EQUAT", "ALLSKY"]
    assert d["caption"].startswith("AARO RC16")
    assert tf.parse_data((FIX / "documented-format_fit.txt").read_text(encoding="ascii")) is None
    assert tf.parse_data("hello\nworld\n") is None


def test_parse_model_terms_rms_and_count():
    m = tf.parse_model((FIX / "documented-format_fit.txt").read_text(encoding="ascii"))
    assert m["terms"]["IH"] == {"value": -3484.63, "sigma": 1.23, "fixed": False}
    assert m["terms"]["ME"]["value"] == 120.6 and m["terms"]["MA"]["value"] == -45.2
    assert "HDSH" in m["terms"] and len(m["terms"]) == 8
    assert m["sky_rms_arcsec"] == 15.77 and m["popn_sd_arcsec"] == 16.2
    assert m["observations"] == 84
    # a model file without the change column, and the data file is no model
    m2 = tf.parse_model("RC16 model\n  IH  -3480.10  1.2\n  ME  +60.0  0.9\nEND\n")
    assert m2["terms"]["IH"] == {"value": -3480.1, "sigma": 1.2, "fixed": False}
    assert tf.parse_model((FIX / "documented-format_pointing.dat").read_text(
        encoding="ascii")) is None
    assert tf.parse_model("WIDTH 12 3\nHEIGHT 4 5\n") is None   # not TPOINT terms


def test_protrack_line():
    assert tf.parse_protrack("[x]\nProTrackActive=1\n") is True
    assert tf.parse_protrack("protrack: off") is False
    assert tf.parse_protrack("nothing here") is None


def test_polar_advice_and_limit():
    p = tf.polar({"ME": {"value": 120.6}, "MA": {"value": -45.2}}, 3.0)
    assert p["total_arcmin"] == pytest.approx(2.15, abs=0.01) and p["ok"]
    assert "too high, lower it" in p["advice"][0]
    assert "west of the pole, move it east" in p["advice"][1]
    big = tf.polar({"ME": {"value": -300.0}}, 3.0)
    assert not big["ok"] and "too low, raise it" in big["advice"][0]
    assert tf.polar({"IH": {"value": 1.0}}) is None


# ------------------------------------------------------------- files

def test_listing_finds_the_tpoint_folder_and_skips_autosave(tmp_path):
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))
    lst = tf.list_files(cfg)
    rels = [f["rel"] for f in lst["files"]]
    assert lst["roots"] == [{"path": str(root), "exists": True}]
    assert all(r.startswith("TPoint") for r in rels) and len(rels) == 4
    assert not any("AutoSave" in r for r in rels)
    assert rels[-1].endswith("old_run.dat")                 # newest first


def test_default_roots_include_reinstall_folders(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / "Documents" / "Software Bisque" / "TheSky Professional Edition 64 2").mkdir(
        parents=True)
    rs = [str(r) for r in tf.roots(_cfg(tmp_path))]
    assert any(r.endswith("TheSky Professional Edition 64") for r in rs)
    assert any(r.endswith("TheSky Professional Edition 64 2") for r in rs)


def test_stats_merge_newest_data_and_model(tmp_path):
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))
    st = tf.stats(cfg)
    assert st["ok"] and st["points"] == 5          # the newest data file
    assert st["data_file"].endswith("documented-format_pointing.dat")
    assert st["data_date"] == "2026-10-09"
    assert st["sky_rms_arcsec"] == 15.77 and st["terms"]["ID"]["value"] == 52.41
    assert st["polar"]["total_arcmin"] == pytest.approx(2.15, abs=0.01)
    assert st["protrack"] is True
    text = tf.format_stats(st)
    assert "5 points (84 in the fit), sky RMS 15.77\"" in text
    assert "IH -3484.6\", ID +52.4\", ME +120.6\", MA -45.2\"" in text
    assert "polar alignment: 2.15' (OK the 3' limit)" in text
    assert "ProTrack: on" in text


def test_stats_without_files_says_where_it_looked(tmp_path):
    st = tf.stats(_cfg(tmp_path, thesky_user_dir=str(tmp_path / "nope")))
    assert not st["ok"] and "set thesky_user_dir" in st["note"]
    assert "nothing found" not in tf.format_stats(st)


def test_head_only_for_listed_files(tmp_path):
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))
    h = tf.head(cfg, str(Path("TPoint") / "documented-format_fit.txt"), 100)
    assert h["ok"] and h["text"].startswith("TPOINT fit report") and h["truncated"]
    assert not tf.head(cfg, "..\\..\\secret.txt")["ok"]
    assert not tf.head(cfg, str(Path("Camera AutoSave") / "Imager"))["ok"]


def test_save_history_and_the_drift_trend_key(tmp_path, monkeypatch):
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))
    st = tf.stats(cfg)
    tf.save(cfg, st, datetime(2026, 10, 9, 18, 0))
    tf.save(cfg, st, datetime(2026, 10, 9, 19, 0))      # unchanged: one line
    assert len(tf.history(cfg)) == 1
    st2 = dict(st, points=140, sky_rms_arcsec=11.2, model_date="2026-10-12")
    tf.save(cfg, st2, datetime(2026, 10, 12, 18, 0))
    assert [h["points"] for h in tf.history(cfg)] == [5, 140]
    model_date = st["model_date"]
    assert tf.model_for_night(cfg, "2026-10-11")["model_date"] == model_date
    assert tf.model_for_night(cfg, "2026-10-13")["points"] == 140
    assert tf.model_for_night(cfg, "2000-01-01") == {}
    from photonscript.scheduler import thesky_audit as ta
    from photonscript.scheduler import tracking_drift as td
    monkeypatch.setattr(ta, "load_night", lambda c, n: None)
    m = td.tpoint_model(cfg, "2026-10-13")
    assert m["points"] == 140 and m["source"] == "TPoint files"


def test_audit_reads_the_files_as_a_source(tmp_path, monkeypatch):
    from photonscript.scheduler import thesky_audit as ta
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))
    assert "tpoint-files" in ta.SOURCES
    monkeypatch.setattr(ta, "_local_thesky", lambda c: True)
    from photonscript.scheduler import thesky_procs
    monkeypatch.setattr(thesky_procs, "scan", lambda c: {"ok": False})
    obs = ta.collect(cfg)
    o = obs["tpoint-files"]
    assert o["tpoint_points"] == 5 and o["tpoint_rms_arcsec"] == 15.77
    assert o["tpoint_polar_error_arcmin"] == pytest.approx(2.15, abs=0.01)
    assert obs["_sources"]["tpoint-files"]["ok"]
    desired = ta.load_desired(cfg)
    rows = {r["id"]: r for r in desired["check"]}
    assert "tpoint-files" in rows["tpoint_points"]["sources"]
    res = ta.evaluate(desired, obs, cfg)
    pts = next(r for r in res["rows"] if r["id"] == "tpoint_points")
    assert pts["source"] == "tpoint-files" and pts["current"] in ("5", 5)
    monkeypatch.setattr(ta, "_local_thesky", lambda c: False)
    assert not ta.collect(cfg)["_sources"]["tpoint-files"]["ok"]


def test_routes(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import thesky as rt
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    st = asyncio.run(rt.api_thesky_tpoint())
    assert st["points"] == 5 and "polar alignment" in st["text"]
    lst = asyncio.run(rt.api_thesky_tpoint_files())
    assert lst["files"] and all("path" not in f and "_ts" not in f for f in lst["files"])
    rel = lst["files"][0]["rel"]
    assert asyncio.run(rt.api_thesky_tpoint_file(rel=rel, max_kb=1))["ok"]
    r = asyncio.run(rt.api_thesky_tpoint_file(rel="C:\\Windows\\win.ini"))
    assert r.status_code == 404


def test_cli_tpoint_stats(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    r = CliRunner().invoke(cli.app, ["tpoint-stats", "--files"])
    assert r.exit_code == 0
    assert "5 points" in r.output and "lower it" in r.output
    assert "documented-format_fit.txt" in r.output


def test_probe_shows_the_file_numbers_and_their_change(tmp_path):
    from photonscript.telescope_agent import thesky_client as tc
    from photonscript.telescope_agent import tpoint_sample as ts

    class _Down(tc.TheSkyClient):
        def run_script(self, js):
            raise tc.TheSkyError("refused")
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, thesky_user_dir=str(root))

    class _Up(tc.TheSkyClient):
        def run_script(self, js):
            return "act_type_execute=function"
    r1 = ts.run_probe(cfg, client=_Up("x", 1), now=datetime(2026, 10, 9, 18, 0))
    assert r1["files"]["points"] == 5
    (root / "TPoint" / "documented-format_fit.txt").write_text(
        (FIX / "documented-format_fit.txt").read_text(encoding="ascii")
        .replace("15.77", "11.40"), encoding="ascii")
    r2 = ts.run_probe(cfg, client=_Up("x", 1), now=datetime(2026, 10, 10, 18, 0))
    text = ts.format_probe(r2)
    assert "sky RMS 11.4\"" in text and "sky_rms_arcsec was 15.77" in text
    down = ts.run_probe(cfg, client=_Down("x", 1))
    assert "FAILED" in ts.format_probe(down) and "5 points" in ts.format_probe(down)


# ------------------------------------------------- real TheSky64 files

REAL = {n.name[len("thesky64_"):]: n for n in FIX.glob("thesky64_*")}
TERMS_233 = ["IH", "ID", "HHSH", "HHSH2", "HHSH3", "HDSD", "HDCD", "HDCD2",
             "HDCD3", "HDSD7", "HDSH", "HDSHSD", "HDCH", "HDCH3", "NP", "CH",
             "HXCD2", "HXSHCD7", "DAF", "ME", "MA", "TF", "TX10"]


def _real(name):
    return REAL[name].read_bytes().decode("latin-1")


def test_real_outmod_rms_terms_and_fixed_np():
    m = tf.parse_model(_real("TPoint base run outmod.dat"))
    assert m["observations"] == 233 and m["sky_rms_arcsec"] == 15.7683
    assert list(m["terms"]) == TERMS_233                      # file order, all 23
    assert m["terms"]["NP"] == {"value": 0.0, "sigma": None, "fixed": True}
    assert m["terms"]["HHSH2"]["value"] == -59.6319           # "& HHSH2" line
    assert m["terms"]["TX10"]["value"] == -50.6439
    assert m["terms"]["ME"]["value"] == -57.8537 and m["terms"]["MA"]["value"] == -154.8011
    assert m["fitted_terms"] == 22
    # the T line's last two numbers are the refraction constants, not popn SD
    assert m["refraction"] == {"a_arcsec": 53.048, "b_arcsec": -0.0642}
    assert m["popn_sd_source"] == "derived"
    assert m["popn_sd_arcsec"] == pytest.approx(15.7683 * (233 / 211) ** 0.5, abs=0.01)
    assert tf.parse_data(_real("TPoint base run outmod.dat")) is None


def test_real_recal_model_with_no_data_has_no_rms():
    m = tf.parse_model(_real("TPoint recal run outmod.dat"))
    assert m["sky_rms_arcsec"] is None and m["observations"] is None
    assert m["popn_sd_arcsec"] is None
    assert m["terms"]["NP"] == {"value": 97.4158, "sigma": None, "fixed": True}
    assert m["terms"]["HDSD"]["fixed"] and m["terms"]["HDSH7CD8"]["value"] == 13.9166
    assert m["fitted_terms"] == 2 and len(m["terms"]) == 18


def test_real_data_files_in_and_outdat():
    d = tf.parse_data(_real("TPoint base run in.dat"))
    assert d["points"] == 255 and d["date"] == "2026-10-04"
    assert d["caption"] == "Paramount MX Series 6" and d["options"] == ["NODA", "EQUAT"]
    o = tf.parse_data(_real("Super Model Outdat.dat"))
    assert o["points"] == 233                    # "<obs> & <extra columns>" lines
    assert o["options"] == ["NODA", "ALLSKY", "EQUAT"]
    assert tf.parse_model(_real("Super Model Outdat.dat")) is None


def _real_tree(tmp_path, ages: dict) -> Path:
    root = tmp_path / "Software Bisque" / "TheSkyX Professional Edition"
    tp = root / "TPoint"
    tp.mkdir(parents=True)
    now = time.time()
    for name, age_s in ages.items():
        f = tp / name
        f.write_bytes(REAL[name].read_bytes())
        os.utime(f, (now - age_s, now - age_s))
    return root


def test_real_stats_pairs_the_run_and_formats_everything(tmp_path):
    root = _real_tree(tmp_path, {"TPoint base run outmod.dat": 60,
                                 "TPoint base run in.dat": 60,
                                 "Super Model Outmod.dat": 3600,
                                 "Super Model Outdat.dat": 3600})
    st = tf.stats(_cfg(tmp_path, thesky_user_dir=str(root)))
    assert st["ok"] and st["model_file"].endswith("TPoint base run outmod.dat")
    assert st["data_file"].endswith("TPoint base run in.dat")
    assert st["points"] == 255 and st["model_points"] == 233
    assert st["sky_rms_arcsec"] == 15.7683
    assert st["polar"]["total_arcmin"] == pytest.approx(2.75, abs=0.01)
    assert "confirm with TheSky's Polar Alignment Report" in st["polar"]["note"]
    text = tf.format_stats(st)
    assert "255 points (233 in the fit), sky RMS 15.77\"" in text
    assert "popn SD 16.57\" (derived)" in text
    assert "NP= +0.0\" (all 23 below)" in text.splitlines()[1]   # fixed, not missing
    assert "NP= +0.0\"" in text.splitlines()[5] and "TX10 -50.6\"" in text and "HXSHCD7 -7.4\"" in text
    assert "all 23 terms (22 fitted, = fixed)" in text
    assert "?" not in text.splitlines()[0]


def test_real_stats_prefers_the_super_model_when_newer(tmp_path):
    root = _real_tree(tmp_path, {"TPoint base run outmod.dat": 3600,
                                 "TPoint base run in.dat": 3600,
                                 "Super Model Outmod.dat": 60,
                                 "Super Model Outdat.dat": 60,
                                 "TPoint recal run outmod.dat": 30})
    st = tf.stats(_cfg(tmp_path, thesky_user_dir=str(root)))
    # the newer recal model has no data and no RMS; it still is the newest
    # model, so this run reports it honestly (no data of its own)
    assert st["model_file"].endswith("TPoint recal run outmod.dat")
    root2 = _real_tree(tmp_path / "b", {"TPoint base run outmod.dat": 3600,
                                        "TPoint base run in.dat": 3600,
                                        "Super Model Outmod.dat": 60,
                                        "Super Model Outdat.dat": 60})
    st = tf.stats(_cfg(tmp_path / "b", thesky_user_dir=str(root2)))
    assert st["model_file"].endswith("Super Model Outmod.dat")
    assert st["data_file"].endswith("Super Model Outdat.dat")  # same run, no Indat here
    assert st["points"] == 233 and st["sky_rms_arcsec"] == 15.7683


def test_run_key_groups_in_out_files():
    k = lambda rel: tf.run_key({"rel": rel})
    assert k("TPoint\TPoint base run outmod.dat") == k("TPoint\TPoint base run in.dat")
    assert k("TPoint\Super Model Outdat.dat") == k("TPoint\Super Model Indat.dat")
    assert k("TPoint\Main.dat")[1] == "main"
