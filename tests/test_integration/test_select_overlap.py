"""Piggy-600 sky-overlap selection: mosaic-panel folders join the target."""
from pathlib import Path

from photonscript.integration.frames import Frame
from photonscript.integration.select import select_lights, sep_deg


def _lib(tmp_path: Path, layout: dict) -> Path:
    for rel in layout:
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return tmp_path


def _reader(layout):
    def read(p, kind="LIGHT", tz=""):
        rel = Path(p).relative_to(read.root).as_posix()
        ra, dec, osc = layout[rel]
        return Frame(path=Path(p), exp=300.0, filter="OSC" if osc else "L",
                     bayer="RGGB" if osc else "", date_obs="2026-11-09T02:00:00",
                     night="2026-11-08", ra_deg=ra, dec_deg=dec)
    return read


LAYOUT = {
    "NGC 604/OSC/a.fits": (23.637, 30.783, True),
    "NGC 604/OSC/b.fits": (23.640, 30.780, True),
    "M33 Core P1/OSC/c.fits": (23.664, 30.776, True),     # 1.4' away
    "M33 Core P1/L/d.fits": (23.664, 30.776, False),      # RC16 panel
    "NGC 891/OSC/e.fits": (35.639, 42.349, True),         # elsewhere
}


def _run(tmp_path, rig, **kw):
    lib = _lib(tmp_path, LAYOUT)
    read = _reader(LAYOUT)
    read.root = lib
    return select_lights(lib, "NGC 604", rig, read=read, **kw)


def test_piggy_pulls_in_overlapping_panel(tmp_path):
    sel = _run(tmp_path, "piggyback")
    names = sorted(f.path.name for f in sel.lights)
    assert names == ["a.fits", "b.fits", "c.fits"]
    assert sel.overlap_folders == ["M33 Core P1"]
    assert sel.overlap_count == 1


def test_overlap_off(tmp_path):
    sel = _run(tmp_path, "piggyback", overlap_deg=0)
    assert sorted(f.path.name for f in sel.lights) == ["a.fits", "b.fits"]


def test_rc16_stays_strict(tmp_path):
    lay = dict(LAYOUT)
    lay["NGC 604/L/f.fits"] = (23.637, 30.783, False)
    lib = _lib(tmp_path, lay)
    read = _reader(lay)
    read.root = lib
    sel = select_lights(lib, "NGC 604", "rc16", read=read)
    assert [f.path.name for f in sel.lights] == ["f.fits"]
    assert sel.overlap_folders == []


def test_sep_deg():
    assert abs(sep_deg(10, 0, 11, 0) - 1.0) < 1e-9
    assert sep_deg(23.637, 30.783, 23.664, 30.776) < 0.05


def test_piggy_without_headers_uses_rc16_pointing(tmp_path):
    lay = {
        "NGC 604/OSC/a.fits": (None, None, True),
        "NGC 604/L/r1.fits": (23.637, 30.783, False),
        "M33 Core P1/OSC/c.fits": (None, None, True),
        "M33 Core P1/L/r2.fits": (23.664, 30.776, False),
        "NGC 891/OSC/e.fits": (None, None, True),
        "NGC 891/L/r3.fits": (35.639, 42.349, False),
    }
    lib = _lib(tmp_path, lay)
    read = _reader(lay)
    read.root = lib
    sel = select_lights(lib, "NGC 604", "piggyback", read=read)
    assert sorted(f.path.name for f in sel.lights) == ["a.fits", "c.fits"]
    assert sel.overlap_folders == ["M33 Core P1"]
