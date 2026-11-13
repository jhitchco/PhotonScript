"""PS-22: picking approved lights from the Library mirror."""
from pathlib import Path

import pytest

from photonscript.integration import select as s
from photonscript.integration.frames import Frame, from_header, night_of, parse_time


def _lib(tmp_path, files):
    lib = tmp_path / "Library"
    for rel in files:
        p = lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return lib


def _reader(meta):
    """Fake header reader: meta maps file name -> Frame field overrides."""
    def read(p, kind=None, tz=None):
        m = {"exp": 120.0, "bayer": "RGGB", "filter": "OSC", "date_obs": "2026-09-21T10:00:00",
             "night": "2026-09-20", "instrument": "AP26CC"}
        m.update(meta.get(Path(p).name, {}))
        return Frame(path=Path(p), kind=kind or "LIGHT", **m)
    return read


def test_target_folders_join_aliases_ps78(tmp_path):
    lib = _lib(tmp_path, ["Andromeda Galaxy/OSC/a.fits", "M 31/OSC/b.fits", "Crescent Nebula/Ha/c.fits",
                          "_rejected/M 31/OSC/r.fits", "BAD/OSC/x.fits",
                          "piggyback/Calibration/DARK/2026-09-12/d.fits"])
    names = [d.name for d in s.target_folders(lib, "Andromeda Galaxy")]
    assert names == ["Andromeda Galaxy", "M 31"]
    assert [d.name for d in s.target_folders(lib, "M31")] == ["Andromeda Galaxy", "M 31"]
    assert [d.name for d in s.target_folders(lib, "Crescent Nebula")] == ["Crescent Nebula"]


def test_container_named_folder_counts_for_its_target(tmp_path):
    lib = _lib(tmp_path, ["Heart Nebula/Ha/a.fits",
                          "Heart Nebula imaging (repeats while safe and up)_Container/Ha/b.fits"])
    assert len(s.target_folders(lib, "Heart Nebula")) == 2


def test_select_piggyback_only_osc_and_window(tmp_path):
    lib = _lib(tmp_path, ["Andromeda Galaxy/OSC/a.fits", "Andromeda Galaxy/OSC/b.fits",
                          "M 31/OSC/c.fits", "M 31/OSC/notes.txt", "Andromeda Galaxy/Ha/h.fits",
                          "_rejected/Andromeda Galaxy/OSC/r.fits"])
    read = _reader({"b.fits": {"night": "2026-09-01"},
                    "c.fits": {"exp": 400.0, "night": "2026-10-03", "date_obs": "2026-10-04T03:00:00"}})
    sel = s.select_lights(lib, "Andromeda Galaxy", "piggyback", since="2026-09-10", read=read)
    assert [f.name for f in sel.lights] == ["a.fits", "c.fits"]
    assert any("before --since" in why for _, why in sel.skipped)
    g = sel.groups()
    assert list(g) == [("OSC", "120s"), ("OSC", "400s")]
    assert sel.lights[1].target_dir == "M 31"


def test_select_rc16_skips_osc_folder_and_filters(tmp_path):
    lib = _lib(tmp_path, ["Crescent Nebula/Ha/a.fits", "Crescent Nebula/OIII/b.fits",
                          "Crescent Nebula/OSC/c.fits"])
    read = _reader({n: {"bayer": "", "filter": f, "instrument": "AP26MC"}
                    for n, f in (("a.fits", "Ha"), ("b.fits", "OIII"))})
    sel = s.select_lights(lib, "Crescent Nebula", "rc16", read=read)
    assert sorted(f.name for f in sel.lights) == ["a.fits", "b.fits"]
    sel2 = s.select_lights(lib, "Crescent Nebula", "rc16", filters=["Ha"], read=read)
    assert [f.name for f in sel2.lights] == ["a.fits"]


def test_select_rejects_a_mono_frame_in_the_osc_folder(tmp_path):
    lib = _lib(tmp_path, ["M 31/OSC/a.fits"])
    sel = s.select_lights(lib, "M 31", "piggyback", read=_reader({"a.fits": {"bayer": ""}}))
    assert not sel.lights and "rig rc16" in sel.skipped[0][1]


def test_duplicate_names_count_once(tmp_path):
    lib = _lib(tmp_path, ["Andromeda Galaxy/OSC/a.fits", "M 31/OSC/a.fits"])
    sel = s.select_lights(lib, "M31", "piggyback", read=_reader({}))
    assert len(sel.lights) == 1


def test_unknown_rig():
    with pytest.raises(ValueError):
        s.select_lights(Path("."), "M31", "seestar")


def test_library_is_never_written(tmp_path):
    lib = _lib(tmp_path, ["M 31/OSC/a.fits"])
    before = {p: p.stat().st_mtime_ns for p in lib.rglob("*")}
    s.select_lights(lib, "M31", "piggyback", read=_reader({}))
    assert {p: p.stat().st_mtime_ns for p in lib.rglob("*")} == before


# ----------------------------------------------------------- frames

def test_night_of_evening_date():
    assert night_of("2026-09-21T03:00:19.1234567") == "2026-09-20"
    assert night_of("2026-10-03T20:01:24.3268722") == "2026-10-03"
    assert night_of("", "2026-10-04T02:01:24") == "2026-10-03"      # UTC -> Denver
    assert night_of("", "") == ""
    assert parse_time("2026-10-03T20:01:24.3268722").microsecond == 326872


def test_from_header_osc_and_readout():
    h = {"IMAGETYP": "LIGHT", "EXPTIME": 300.0, "GAIN": 100, "OFFSET": 256, "SET-TEMP": 0.0,
         "READOUTM": "Low Conversion Gain", "INSTRUME": "AP26CC", "FILTER": None, "BAYERPAT": "RGGB",
         "DATE-LOC": "2026-10-03T20:01:24.3", "XBINNING": 1, "FOCALLEN": 600.0, "FOCRATIO": 5.6}
    f = from_header("x/2026.fits", h)
    assert f.filter == "OSC" and f.is_osc and f.readout == "LCG" and f.readout_raw == "Low Conversion Gain"
    assert f.exp_key == "300s" and f.night == "2026-10-03" and f.kind == "LIGHT"
    d = from_header("d.fits", {"IMAGETYP": "Dark Frame", "EXPTIME": 0.14})
    assert d.kind == "DARK" and d.exp_key == "0.14s" and d.filter == ""
