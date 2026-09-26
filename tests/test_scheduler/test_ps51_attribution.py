"""PS-51: every sub gets a campaign target, and the library follows it."""

import numpy as np
from astropy.io import fits

from photonscript.scheduler import identify
from photonscript.scheduler.runs import (
    _load_subs,
    append_sub_record,
    attribute_night,
    build_library,
    library_root,
)
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-09-26"
CRESCENT = ("Crescent Nebula", 303.03, 38.355, True)
CATS_EYE = ("Cat's Eye Nebula", 269.64, 66.633, True)


def _config(tmp_path):
    return PhotonScriptConfig(data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _fits(path, ra=None, dec=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(np.zeros((8, 8), dtype=np.uint16))
    hdu.header["IMAGETYP"] = "LIGHT"
    if ra is not None:
        hdu.header["RA"] = ra
        hdu.header["DEC"] = dec
    hdu.writeto(path, overwrite=True)
    return path


def _sub(config, rel, *, target="?", filt="Ha", rig="rc16",
         time="2026-09-26T07:37:03", ok=True):
    append_sub_record(config, NIGHT, {
        "rig": rig, "file": rel,
        "abs_path": str(config.image_watch_dir) + "/" + NIGHT + "/" + rel,
        "time": time, "target": target, "filter": filt, "exp_s": 900,
        "passed_qa": ok, "reviewed": ok, "reason": ""})


def test_match_prefers_project_over_catalog():
    # a catalog neighbour closer to the pointing must not steal a project sub
    cands = [("M 32", 10.67, 40.87, False), ("Andromeda Galaxy", 10.68, 41.27, True)]
    assert identify.match_target(10.67, 40.90, cands) == "Andromeda Galaxy"
    # nothing within the radius -> None
    assert identify.match_target(100.0, -40.0, cands) is None


def test_target_from_header_live(monkeypatch):
    monkeypatch.setattr(identify, "cached_candidates",
                        lambda config: [CRESCENT, CATS_EYE])
    assert identify.target_from_header(None, {"RA": 303.057, "DEC": 38.326}) \
        == "Crescent Nebula"
    assert identify.target_from_header(None, {"OBJCTRA": "17 58 38",
                                              "OBJCTDEC": "66 35 09"}) \
        == "Cat's Eye Nebula"
    # Piggy-600 frames carry no coordinates
    assert identify.target_from_header(None, {"INSTRUME": "AP26CC"}) is None


def test_build_library_names_subs_and_moves_stale_links(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(identify, "_candidates",
                        lambda config: [CRESCENT, CATS_EYE])
    watch = tmp_path / "fits" / NIGHT
    _fits(watch / "LIGHT" / "H_0000.fits", 303.057, 38.326)
    _fits(watch / "LIGHT" / "O_0000.fits", 269.658, 66.586)
    _sub(config, "LIGHT/H_0000.fits")
    _sub(config, "LIGHT/O_0000.fits", filt="OIII", time="2026-09-26T06:03:17")
    # two targets in one night, OBJECT blank: the case that logged "?"
    first = build_library(config, NIGHT)
    lib = library_root(config)
    assert first["attributed"] == 2
    assert (lib / "Crescent Nebula" / "Ha" / "H_0000.fits").exists()
    assert (lib / "Cat's Eye Nebula" / "OIII" / "O_0000.fits").exists()
    # the header now carries the name (PixInsight and later passes see it)
    assert fits.getheader(watch / "LIGHT" / "H_0000.fits")["OBJECT"] == \
        "Crescent Nebula"
    assert {s["target"] for s in _load_subs(config, NIGHT)} == \
        {"Crescent Nebula", "Cat's Eye Nebula"}


def test_stale_underscore_link_is_moved_not_duplicated(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(identify, "_candidates", lambda config: [CRESCENT])
    watch = tmp_path / "fits" / NIGHT
    src = _fits(watch / "LIGHT" / "H_0001.fits", 303.057, 38.326)
    _sub(config, "LIGHT/H_0001.fits")
    lib = library_root(config)
    stale = lib / "_" / "Ha" / "H_0001.fits"
    stale.parent.mkdir(parents=True)
    stale.write_bytes(src.read_bytes())
    res = build_library(config, NIGHT)
    assert res["retagged"] == 1
    assert not stale.exists()
    assert (lib / "Crescent Nebula" / "Ha" / "H_0001.fits").exists()
    assert src.exists()  # originals are never touched


def test_piggyback_inherits_rc16_target_in_library(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(identify, "_candidates", lambda config: [CATS_EYE])
    watch = tmp_path / "fits" / NIGHT
    _fits(watch / "LIGHT" / "O_0000.fits", 269.658, 66.586)
    _fits(watch / "piggy" / "OSC_0001.fits")  # no coordinates
    _sub(config, "LIGHT/O_0000.fits", filt="OIII", time="2026-09-26T06:03:17")
    _sub(config, "piggy/OSC_0001.fits", filt="OSC", rig="piggyback",
         time="2026-09-26T06:08:00")
    res = build_library(config, NIGHT)
    assert res["attributed"] == 2
    assert (library_root(config) / "Cat's Eye Nebula" / "OSC"
            / "OSC_0001.fits").exists()


def test_library_build_never_plate_solves(tmp_path, monkeypatch):
    config = _config(tmp_path)
    monkeypatch.setattr(identify, "_candidates", lambda config: [CRESCENT])

    def _boom(*a, **k):
        raise AssertionError("ASTAP must not run inside the library build")
    monkeypatch.setattr(identify, "_astap_solve", _boom)
    _fits(tmp_path / "fits" / NIGHT / "piggy" / "OSC_0002.fits")
    _sub(config, "piggy/OSC_0002.fits", filt="OSC", rig="piggyback")
    res = attribute_night(config, NIGHT)
    assert res["attributed"] == 0  # no RC16 anchor, no coords: stays '?'
