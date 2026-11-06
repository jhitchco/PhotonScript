"""Pointing sanity at Library filing (scheduler.library_pointing): before
dawn filing (build_library) or an Approve links a light into
<Target>/<Filter>/, the integrate step's header checks run on it
(integration.pointing_check): more than 1 deg off the target's catalog
position, or the mount not tracking. warn (default): kept out of the target
folder, back to review with the reason, never re-approved at dawn; a
person's Approve of it files it. refuse: rejected. off: as before.
2026-10-13: an "NGC 2408" run held lights whose RA / DEC was M27's."""
from pathlib import Path

import numpy as np
import pytest

from photonscript.scheduler import dawn_autofile as da
from photonscript.scheduler import library_pointing as lp
from photonscript.scheduler import runs
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed

NGC2408 = (112.5, 71.6)        # deg (catalog stand-in)
M27 = (299.9, 22.72)


def _light(path: Path, ra, dec, t, alt=60.0, az=120.0):
    from astropy.io import fits
    h = fits.Header()
    h.update(IMAGETYP="LIGHT", EXPTIME=300.0, FILTER="Ha", OBJECT="NGC 2408",
             RA=ra, DEC=dec, CENTALT=alt, CENTAZ=az, **{"DATE-OBS": t})
    path.parent.mkdir(parents=True, exist_ok=True)
    fits.PrimaryHDU(np.zeros((8, 8), np.uint16), header=h).writeto(path, overwrite=True)
    return str(path)


@pytest.fixture
def night(tmp_path, monkeypatch):
    import photonscript.shared.pointing as sp
    monkeypatch.setattr(sp, "target_coords",
                        lambda cfg, name: ("NGC 2408",) + NGC2408
                        if str(name).lower() == "ngc 2408" else None)

    def make(**kw):
        cfg = _cfg(tmp_path, **kw)
        d = tmp_path / "fits" / NIGHT / "LIGHT"
        recs = [
            _rec("good", target="NGC 2408", reviewed=True, review_source="auto",
                 abs_path=_light(d / "good.fits", *NGC2408, "2026-09-27T04:00:00")),
            _rec("m27", target="NGC 2408", reviewed=True, review_source="auto",
                 abs_path=_light(d / "m27.fits", *M27, "2026-09-27T05:00:00", alt=40.0)),
            # a stopped mount: alt / az pinned while RA ran sidereal (10 min)
            _rec("stop1", target="NGC 2408", reviewed=True, review_source="auto",
                 abs_path=_light(d / "stop1.fits", 112.5, 71.6, "2026-09-27T06:00:00",
                                 alt=50.0, az=10.0)),
            _rec("stop2", target="NGC 2408", reviewed=True, review_source="auto",
                 abs_path=_light(d / "stop2.fits", 112.5 + 2.507, 71.6,
                                 "2026-09-27T06:10:00", alt=50.0, az=10.0)),
        ]
        _seed(cfg, recs)
        return cfg
    return make


def _by_file(cfg):
    return {Path(r["file"]).stem: r for r in runs._load_subs(cfg, NIGHT)}


def _linked(cfg):
    lib = Path(cfg.library_dir)
    return sorted(p.stem for p in lib.rglob("*.fits") if "_rejected" not in p.parts)


def test_dawn_filing_holds_off_target_and_stopped_subs_for_review(night):
    cfg = night()
    res = runs.build_library(cfg, NIGHT)
    assert _linked(cfg) == ["good"]
    assert res["pointing_held"] == 3 and res["linked"] == 1
    recs = _by_file(cfg)
    m27 = recs["m27"]
    assert m27["passed_qa"] is True and m27["reviewed"] is False
    assert m27["review_source"] == "pointing" and lp.held(m27)
    assert m27["reason"].startswith("pointing: off_target") and "NGC 2408" in m27["reason"]
    assert recs["stop1"]["library_pointing"]["issues"][0].startswith("mount_stopped")
    assert not da.approvable(m27)                 # the next dawn never re-approves it
    runs.build_library(cfg, NIGHT)                # a rebuild leaves it out too
    assert _linked(cfg) == ["good"]


def test_a_persons_approve_of_a_held_sub_files_it(night):
    cfg = night()
    runs.build_library(cfg, NIGHT)
    res = runs.set_verdicts(cfg, NIGHT, ["LIGHT/m27.fits"], "accepted")
    assert "pointing_held" not in res
    assert "m27" in _linked(cfg)
    r = _by_file(cfg)["m27"]
    assert r["library_pointing"]["override"] and r["manual_qa"] is True


def test_approve_bounces_a_fresh_off_target_sub_once(night):
    cfg = night()
    with runs.edit_subs(cfg, NIGHT) as subs:      # all waiting for review
        for s in subs:
            s.update(reviewed=False, review_source=None)
    res = runs.set_verdicts(cfg, NIGHT, ["LIGHT/good.fits", "LIGHT/m27.fits"], "accepted")
    assert res["pointing_held"] == ["LIGHT/m27.fits"]
    got = {Path(h["file"]).stem: h for h in res["subs"]}
    assert got["m27"]["reviewed"] is False and got["m27"]["review_source"] == "pointing"
    assert _linked(cfg) == ["good"]
    runs.set_verdicts(cfg, NIGHT, ["LIGHT/m27.fits"], "accepted")   # the second Approve
    assert _linked(cfg) == ["good", "m27"]


def test_refuse_rejects_and_off_files_as_before(night, tmp_path):
    cfg = night(library_pointing_check="refuse")
    runs.build_library(cfg, NIGHT)
    m27 = _by_file(cfg)["m27"]
    assert m27["passed_qa"] is False and m27["auto_verdict"] == "rejected"
    assert _linked(cfg) == ["good"]


def test_off_files_everything(night):
    cfg = night(library_pointing_check="off")
    res = runs.build_library(cfg, NIGHT)
    assert _linked(cfg) == ["good", "m27", "stop1", "stop2"]
    assert res["pointing_held"] == 0


def test_no_ra_dec_and_unknown_target_are_never_held(tmp_path, monkeypatch):
    import photonscript.shared.pointing as sp
    monkeypatch.setattr(sp, "target_coords", lambda cfg, name: None)
    cfg = _cfg(tmp_path)
    d = tmp_path / "fits" / NIGHT / "LIGHT"
    p = _light(d / "x.fits", *M27, "2026-09-27T04:00:00")
    assert lp.screen(cfg, [_rec("x", abs_path=p)], lambda r: "Nowhere") == {}
    assert lp.screen(cfg, [_rec("y", abs_path=str(d / "missing.fits"))],
                     lambda r: "NGC 2408") == {}
