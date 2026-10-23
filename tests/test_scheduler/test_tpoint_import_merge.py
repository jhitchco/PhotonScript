from pathlib import Path

from photonscript.telescope_agent import tpoint_import as ti
from photonscript.scheduler import remote_cli as rc

HDR = ["!TheSky Version 10.5.0 Build 14139 (64 bit)", "Paramount MX", ":NODA", ":EQUAT",
       "+31 54 25 2026 10 20 29.31 870.00 1300.00  0.50 0.5500 0.0065"]


def _f(p: Path, rows):
    p.write_text("".join(x + "\r\n" for x in HDR + rows), encoding="utf-8", newline="")


def test_merge_keeps_one_header_and_all_samples(tmp_path, monkeypatch):
    _f(tmp_path / "20261021T065925Z_west.dat", ["a 1 ", "a 2 "])
    _f(tmp_path / "20261023T035840Z_east.dat", ["b 1 "])
    (tmp_path / "old_merged.dat").write_text("x\r\n", encoding="utf-8")
    monkeypatch.setattr(ti, "check_import_path", lambda p, *a, **k: None)
    r = ti.merge_dir(str(tmp_path), stamp="S")
    assert r["samples"] == 3 and len(r["files"]) == 2
    text = Path(r["out"]).read_bytes().decode()
    assert text.count("Paramount MX") == 1
    assert text.endswith("b 1 \r\n")
    assert text.splitlines()[5:] == ["a 1 ", "a 2 ", "b 1 "]


def test_merge_is_write_gated_remotely():
    c = rc.load_allowlist().commands["tpoint-import-merge"]
    assert c.write_flags == ("--go",)


def test_old_data_file_prefers_super_model_indat_in_oldest_backup(tmp_path):
    a = tmp_path / "20261021T010000Z" / "TPoint"
    b = tmp_path / "20261023T010000Z" / "TPoint"
    a.mkdir(parents=True); b.mkdir(parents=True)
    _f(a / "Super Model Indat.dat", ["o 1 ", "o 2 "])
    _f(b / "Super Model Indat.dat", ["n 1 "])
    assert ti.old_data_file(str(tmp_path)).startswith(str(tmp_path / "20261021T010000Z"))
    assert ti.count_samples(a / "Super Model Indat.dat") == 2
