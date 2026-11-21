"""PS-237: unexpanded "~" in image paths.

2026-09-26 to 10-09 the Librarian's legacy per-sub copy wrote 438 FITS to
C:\\astro\\PhotonScript\\~\\Astrophotography (PS_LOCAL_IMAGE_DIR held a
literal "~"). Every path setting is now expanded at config load, an image
save root that is relative / "~" / inside the checkout is refused with an
alert, "/" in a target name never nests folders, and `tilde-rescue` plans
the unique frames back through the normal ingest without touching them."""
import asyncio
import csv
import os
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from photonscript.shared import paths
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                local_image_dir=str(tmp_path / "local"),
                stacking_output_dir=str(tmp_path / "processed"),
                image_watch_dir=str(tmp_path / "nina"))
    base.update(kw)
    return PhotonScriptConfig(**base)


# ------------------------------------------------------------ expansion

def test_expand_path_tilde_either_separator():
    home = str(Path.home())
    assert paths.expand_path("~/Astrophotography") == home + "/Astrophotography"
    assert paths.expand_path("~\\Astrophotography") == home + "\\Astrophotography"
    assert paths.expand_path("~") == home
    assert paths.expand_path("") == ""
    assert paths.expand_path(None) == ""
    assert paths.expand_path("  C:\\x  ") == "C:\\x"


def test_expand_path_env_vars(monkeypatch):
    monkeypatch.setenv("PS237_ROOT", "D:\\Imgs")
    assert paths.expand_path("%PS237_ROOT%\\Lib") == "D:\\Imgs\\Lib"
    assert paths.expand_path("$PS237_ROOT/Lib") == "D:\\Imgs/Lib"
    # an unknown variable is left as written (the guard catches the rest)
    assert paths.expand_path("%PS237_NOPE%\\x") == "%PS237_NOPE%\\x"


def test_is_path_setting():
    for n in ("data_dir", "db_path", "local_image_dir", "stacking_output_dir",
              "integration_staging_root", "qa_score_weights_file",
              "transfer_key_path"):
        assert paths.is_path_setting(n), n
    for n in ("nina_base_url", "app_name", "nina_filter_names", "directory"):
        assert not paths.is_path_setting(n), n


def test_config_load_expands_every_path_setting(tmp_path, monkeypatch):
    home = Path.home()
    monkeypatch.setenv("PS_STACKING_OUTPUT_DIR", "~/Astrophotography/Processed")
    monkeypatch.setenv("PS237_LIB", str(tmp_path / "lib"))
    cfg = PhotonScriptConfig(_env_file=None,
                             local_image_dir="~\\Astrophotography",
                             library_dir="%PS237_LIB%",
                             data_dir="~/.photonscript-test",
                             app_name="~keep")
    assert cfg.local_image_dir == str(home) + "\\Astrophotography"
    assert cfg.stacking_output_dir == str(home) + "/Astrophotography/Processed"
    assert cfg.library_dir == str(tmp_path / "lib")
    assert isinstance(cfg.data_dir, Path) and cfg.data_dir == home / ".photonscript-test"
    assert cfg.app_name == "~keep"          # not a path setting
    for name in PhotonScriptConfig.model_fields:
        if paths.is_path_setting(name):
            assert not str(getattr(cfg, name)).startswith("~"), name


def test_config_reads_tilde_from_env_file(tmp_path):
    env = tmp_path / ".env"
    env.write_text('PS_LOCAL_IMAGE_DIR="~\\\\Astrophotography"\n', encoding="utf-8")
    cfg = PhotonScriptConfig(_env_file=str(env))
    assert "~" not in cfg.local_image_dir
    assert Path(cfg.local_image_dir) == Path.home() / "Astrophotography"


def test_example_env_has_no_raw_tilde():
    text = (paths.REPO_ROOT / "config" / "example.env").read_text(encoding="utf-8")
    for line in text.splitlines():
        if line.startswith("PS_") and "=" in line:
            assert '="~' not in line, line


# ------------------------------------------------------------ save-root guard

@pytest.mark.parametrize("value", [
    "~\\Astrophotography", "~/Astrophotography", "Astrophotography",
    "sub\\dir", ".\\x", "C:\\astro\\~\\Astrophotography"])
def test_save_root_refuses_relative_and_tilde(value):
    assert paths.save_root_problem(value)


def test_save_root_refuses_inside_repo():
    why = paths.save_root_problem(str(paths.REPO_ROOT / "~" / "Astrophotography"))
    assert why
    why = paths.save_root_problem(str(paths.REPO_ROOT / "images"))
    assert "inside the PhotonScript checkout" in why


def test_save_root_accepts_absolute_outside_repo(tmp_path):
    assert paths.save_root_problem(str(tmp_path / "Astrophotography")) is None
    assert paths.save_root_problem("") is None
    assert paths.save_root_problem("D:\\Astrophotography") is None


def test_image_root_problems_names_the_setting(tmp_path):
    cfg = _cfg(tmp_path, stacking_output_dir="Processed")
    bad = paths.image_root_problems(cfg)
    assert list(bad) == ["PS_STACKING_OUTPUT_DIR"]
    assert paths.image_root_problems(_cfg(tmp_path)) == {}


def test_preflight_save_roots_check(tmp_path):
    from photonscript.scheduler.preflight import save_roots_check
    cfg = _cfg(tmp_path)
    assert save_roots_check(cfg, {"rc16": str(tmp_path / "N.I.N.A")})["status"] == "pass"
    r = save_roots_check(cfg, {"rc16": "~\\Astrophotography", "piggyback": ""})
    assert r["status"] == "fail" and "NINA (rc16)" in r["detail"]
    r = save_roots_check(_cfg(tmp_path, local_image_dir="Astro"), {})
    assert r["status"] == "fail" and "PS_LOCAL_IMAGE_DIR" in r["detail"]


def _no_loop(monkeypatch, cls, name):
    async def _noop(self):
        return None
    monkeypatch.setattr(cls, name, _noop)


def test_librarian_refuses_relative_root_and_alerts(tmp_path, monkeypatch):
    from photonscript.librarian import agent as lib
    monkeypatch.chdir(tmp_path)
    alerts = []

    async def _refuse(config, setting, why, who):
        alerts.append((setting, why))
    monkeypatch.setattr(lib, "refuse_save_root", _refuse)
    _no_loop(monkeypatch, lib.Librarian, "_transfer_loop")
    cfg = _cfg(tmp_path, local_image_dir="Astrophotography",
               librarian_legacy_transfer=True)
    lb = lib.Librarian(cfg)
    asyncio.run(lb.start())
    assert alerts and alerts[0][0] == "PS_LOCAL_IMAGE_DIR"
    assert lb._legacy_transfer is False
    assert not (tmp_path / "Astrophotography").exists()


def test_librarian_good_root_is_used(tmp_path, monkeypatch):
    from photonscript.librarian import agent as lib
    alerts = []

    async def _refuse(*a):
        alerts.append(a)
    monkeypatch.setattr(lib, "refuse_save_root", _refuse)
    _no_loop(monkeypatch, lib.Librarian, "_transfer_loop")
    cfg = _cfg(tmp_path, librarian_legacy_transfer=True)
    lb = lib.Librarian(cfg)
    asyncio.run(lb.start())
    assert not alerts and lb._legacy_transfer is True
    assert (tmp_path / "local").is_dir()


def test_processor_refuses_in_repo_root_and_makes_nothing(tmp_path, monkeypatch):
    from photonscript.image_processor import agent as proc
    alerts = []

    async def _refuse(config, setting, why, who):
        alerts.append(setting)
    monkeypatch.setattr(proc, "refuse_save_root", _refuse)
    _no_loop(monkeypatch, proc.ImageProcessor, "_processing_loop")
    inside = paths.REPO_ROOT / "~" / "Astrophotography" / "Processed-ps237-test"
    p = proc.ImageProcessor(_cfg(tmp_path, stacking_output_dir=str(inside)))
    asyncio.run(p.start())
    assert alerts == ["PS_STACKING_OUTPUT_DIR"]
    assert not inside.exists()
    p._image_groups = {"M31": {"L": ["a", "b", "c"]}}
    asyncio.run(p._stack_filter("M31", "L"))   # refused root: no stack
    assert not inside.exists()


def test_processor_start_does_not_create_output(tmp_path, monkeypatch):
    from photonscript.image_processor import agent as proc
    _no_loop(monkeypatch, proc.ImageProcessor, "_processing_loop")
    p = proc.ImageProcessor(_cfg(tmp_path))
    asyncio.run(p.start())
    assert not (tmp_path / "processed").exists()


# ------------------------------------------------------------ folder names

def test_tpoint_name_stays_one_folder(tmp_path):
    from photonscript.librarian.agent import transfer_dest
    from photonscript.shared.models import CapturedImage
    name = "TPoint point 01/45 alt 38.1 az 72.6 (east)"
    assert paths.safe_component(name) == "TPoint point 01_45 alt 38.1 az 72.6 (east)"
    assert paths.safe_component("a\\b") == "a_b"
    assert paths.safe_component("..") == paths.UNKNOWN_DIR
    img = CapturedImage(project_id="", filename="x.fits", file_path="x.fits",
                        target_name=name, filter_type="L", exposure_seconds=5)
    dest = transfer_dest(tmp_path, img)
    assert dest.parent.parent.parent == tmp_path       # target/filter/file


def test_processor_stack_folder_sanitized(tmp_path, monkeypatch):
    from photonscript.image_processor import agent as proc

    class _R:
        returncode = 1
        stderr = "no siril"
    monkeypatch.setattr(proc.subprocess, "run", lambda *a, **k: _R())
    p = proc.ImageProcessor(_cfg(tmp_path))
    name = "TPoint point 01/45 alt 38.1 az 72.6 (east)"
    p._image_groups = {name: {"L": ["a", "b", "c"]}}
    asyncio.run(p._stack_filter(name, "L"))
    made = [d.name for d in (tmp_path / "processed").iterdir()]
    assert made == ["TPoint point 01_45 alt 38.1 az 72.6 (east)"]


# ------------------------------------------------------------ tilde-rescue

def _fits(path: Path, **hdr):
    path.parent.mkdir(parents=True, exist_ok=True)
    h = fits.Header()
    for k, v in hdr.items():
        h[k.replace("_", "-")] = v
    fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16), header=h).writeto(path)
    return path


def _tree(tmp_path):
    src = tmp_path / "PhotonScript" / "~" / "Astrophotography"
    a = _fits(src / "Heart" / "Ha" / "heart_ha_001.fits", OBJECT="Heart",
              FILTER="Ha", EXPTIME=300.0, DATE_OBS="2026-09-27T05:10:00",
              IMAGETYP="LIGHT", FOCALLEN=3290.0)
    b = _fits(src / "M 31" / "OSC" / "m31_piggy_001.fits", OBJECT="M 31",
              FILTER="OSC", EXPTIME=300.0, DATE_OBS="2026-10-04T04:00:00",
              IMAGETYP="LIGHT", FOCALLEN=600.0)
    c = _fits(src / "TPoint point 01" / "45 alt 38.1 az 72.6 (east)" / "L"
              / "tp_001.fits", OBJECT="TPoint point 01/45 alt 38.1 az 72.6 (east)",
              FILTER="L", EXPTIME=5.0, DATE_OBS="2026-10-10T03:00:00",
              IMAGETYP="LIGHT", FOCALLEN=3290.0)
    d = _fits(src / "Andromeda" / "L" / "dup_001.fits", OBJECT="Andromeda",
              FILTER="L", EXPTIME=120.0, DATE_OBS="2026-10-01T04:00:00",
              IMAGETYP="LIGHT", FOCALLEN=3290.0)
    return src, [a, b, c, d]


def _csv(path: Path, rows: list[dict]):
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    return path


def _snapshot(root: Path) -> dict:
    return {str(p): p.stat().st_size for p in root.rglob("*") if p.is_file()}


def test_rescue_csv_unique_column(tmp_path):
    from photonscript.librarian.tilde_rescue import read_csv_rows
    p = _csv(tmp_path / "t.csv", [
        {"FullName": "a.fits", "Length": "10", "Unique": "True"},
        {"FullName": "b.fits", "Length": "11", "Unique": "False"}])
    rows, note = read_csv_rows(p)
    assert [r["path"] for r in rows] == ["a.fits"] and rows[0]["size"] == 10
    assert "1 of 2" in note
    p = _csv(tmp_path / "s.csv", [
        {"Path": "a.fits", "Status": "nowhere else"},
        {"Path": "b.fits", "Status": "Library"}])
    rows, _ = read_csv_rows(p)
    assert [r["path"] for r in rows] == ["a.fits"]


def test_rescue_plan_is_read_only_and_routes_by_rig(tmp_path):
    from photonscript.librarian import tilde_rescue as tr
    src, (a, b, c, d) = _tree(tmp_path)
    lib = tmp_path / "data" / "Library" / "Andromeda" / "L"
    lib.mkdir(parents=True)
    (lib / "dup_001.fits").write_bytes(d.read_bytes())       # same size: a dup
    rows = [{"FullName": str(p), "Length": p.stat().st_size} for p in (a, b, c, d)]
    rows.append({"FullName": str(src / "gone.fits"), "Length": 1})
    csv_p = _csv(tmp_path / "check.csv", rows)
    cfg = _cfg(tmp_path)
    before = _snapshot(tmp_path)
    res = tr.plan(cfg, src, csv_p)
    assert _snapshot(tmp_path) == before                    # nothing written
    by = {Path(i["source"]).name: i for i in res["items"]}
    assert by["heart_ha_001.fits"]["action"] == "copy"
    assert by["heart_ha_001.fits"]["rig"] == "rc16"
    assert by["heart_ha_001.fits"]["night"] == "2026-09-26"
    assert by["heart_ha_001.fits"]["dest"] == str(
        tmp_path / "nina" / "2026-09-26" / "Heart" / "LIGHT" / "heart_ha_001.fits")
    assert by["m31_piggy_001.fits"]["rig"] == "piggyback"
    assert by["m31_piggy_001.fits"]["action"] == "hold"
    tp = by["tp_001.fits"]
    assert tp["action"] == "copy"
    assert "TPoint calibration run" in " ".join(tp["notes"])
    assert "/" not in Path(tp["dest"]).parent.parent.name
    assert by["dup_001.fits"]["action"] == "skip"
    assert by["gone.fits"]["action"] == "skip"
    assert res["counts"] == {"copy": 2, "hold": 1, "skip": 2}
    assert res["dry_run"] is True


def test_rescue_apply_copies_never_moves(tmp_path):
    from photonscript.librarian import tilde_rescue as tr
    src, files = _tree(tmp_path)
    csv_p = _csv(tmp_path / "check.csv",
                 [{"FullName": str(p), "Length": p.stat().st_size} for p in files])
    cfg = _cfg(tmp_path)
    res = tr.plan(cfg, src, csv_p)
    out = tr.apply(cfg, res)
    assert len(out["copied"]) == 3 and not out["failed"]   # 2 RC16 + Andromeda
    for p in files:
        assert p.exists()                                  # source kept
    again = tr.apply(cfg, res)                             # never overwrites
    assert not again["copied"] and len(again["failed"]) == 3


def test_rescue_cli_dry_run_json(tmp_path, monkeypatch):
    import json
    from typer.testing import CliRunner
    from photonscript.cli import app
    src, files = _tree(tmp_path)
    csv_p = _csv(tmp_path / "check.csv",
                 [{"FullName": str(p), "Length": p.stat().st_size} for p in files])
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PS_IMAGE_WATCH_DIR", str(tmp_path / "nina"))
    before = _snapshot(tmp_path)
    r = CliRunner().invoke(app, ["tilde-rescue", "--source", str(src),
                                 "--csv", str(csv_p), "--json"])
    assert r.exit_code == 0, r.output
    res = json.loads(r.output)
    assert res["dry_run"] is True and res["counts"]["copy"] == 3
    assert _snapshot(tmp_path) == before


def test_rescue_cli_apply_refused_while_service_runs(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript.cli import app
    from photonscript.librarian import tilde_rescue as tr
    src, files = _tree(tmp_path)
    csv_p = _csv(tmp_path / "check.csv",
                 [{"FullName": str(p)} for p in files])
    monkeypatch.setenv("PS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("PS_IMAGE_WATCH_DIR", str(tmp_path / "nina"))
    monkeypatch.setattr(tr, "service_answers", lambda config, timeout=2.0: True)
    before = _snapshot(tmp_path)
    r = CliRunner().invoke(app, ["tilde-rescue", "--source", str(src),
                                 "--csv", str(csv_p), "--apply"])
    assert r.exit_code == 2
    assert _snapshot(tmp_path) == before
