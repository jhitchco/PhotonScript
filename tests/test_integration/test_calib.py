"""PS-22: calibration matching (bias / darks / flats) on synthetic frames."""
from pathlib import Path

from photonscript.integration import calib
from photonscript.integration.frames import Frame


def _f(name, kind="LIGHT", exp=120.0, gain=100, offset=256, temp=0.0, ro="LCG", inst="AP26CC",
       filt="OSC", session="", night="2026-10-03", xbin=1, bayer="RGGB"):
    return Frame(path=Path(name), kind=kind, exp=exp, gain=gain, offset=offset, set_temp=temp,
                 readout=ro, instrument=inst, filter=filt, session=session, night=night,
                 xbin=xbin, bayer=bayer)


def _many(n, **kw):
    return [_f(f"{kw.get('kind', 'X')}_{kw.get('session', '')}_{kw.get('exp', 0)}_{i}.fits", **kw)
            for i in range(n)]


EP = calib.Epoch("AP26CC", 100, 256, 1, 0.0, "LCG")


def test_epoch_reason_each_field():
    ok = _f("d.fits", kind="DARK")
    assert calib.epoch_reason(ok, EP) == ""
    assert calib.epoch_reason(_f("d", inst="AP26MC"), EP).startswith("camera")
    assert calib.epoch_reason(_f("d", gain=200), EP) == "gain 200"
    assert calib.epoch_reason(_f("d", offset=50), EP) == "offset 50"
    assert calib.epoch_reason(_f("d", xbin=2), EP) == "bin 2"
    assert calib.epoch_reason(_f("d", temp=5.0), EP).startswith("temperature")
    assert calib.epoch_reason(_f("d", temp=1.0), EP) == ""          # within 1.5 C


def test_readout_must_match_ps128():
    assert calib.epoch_reason(_f("d", ro="HCG"), EP) == "readout HCG"
    # no READOUTM in the header: assumed at the rig's default readout
    assert calib.epoch_reason(_f("d", ro=None), EP, default_readout="LCG") == ""
    assert calib.epoch_reason(_f("d", ro=None), EP, default_readout="HCG") == "readout HCG"


def test_bias_newest_session_first_and_capped():
    cals = _many(30, kind="BIAS", exp=0.0, session="2026-07-01") + \
        _many(30, kind="BIAS", exp=0.0, session="2026-09-12")
    b = calib.match_bias(cals, EP, max_frames=40)
    assert len(b) == 40
    assert sum(1 for x in b if x.session == "2026-09-12") == 30


def test_darks_exact_length_used_as_is():
    cals = _many(20, kind="DARK", exp=300.0, session="2026-10-04") + \
        _many(50, kind="DARK", exp=120.0, session="2026-09-12")
    d = calib.match_darks(cals, EP, [300.0, 120.0], have_bias=True)
    assert d[300.0].dark_exp == 300.0 and not d[300.0].scaled and len(d[300.0].frames) == 20
    assert d[120.0].dark_exp == 120.0 and not d[120.0].scaled


def test_darks_scaled_when_length_missing_m31_v4():
    cals = _many(50, kind="DARK", exp=120.0, session="2026-09-12")
    d = calib.match_darks(cals, EP, [400.0, 300.0, 120.0], have_bias=True)
    assert d[400.0].scaled and d[400.0].dark_exp == 120.0
    assert d[300.0].scaled
    assert not d[120.0].scaled
    assert "scaled" in d[400.0].note


def test_no_scaling_without_bias():
    cals = _many(50, kind="DARK", exp=120.0)
    d = calib.match_darks(cals, EP, [400.0], have_bias=False)
    assert not d[400.0].frames and "no bias" in d[400.0].note


def test_too_few_darks_reported():
    cals = _many(4, kind="DARK", exp=120.0)
    d = calib.match_darks(cals, EP, [120.0], have_bias=True, min_darks=10)
    assert not d[120.0].frames and "too few darks" in d[120.0].note


def test_off_epoch_darks_ignored():
    cals = _many(50, kind="DARK", exp=120.0, gain=200) + _many(50, kind="DARK", exp=120.0, ro="HCG")
    d = calib.match_darks(cals, EP, [120.0], have_bias=True)
    assert not d[120.0].frames and d[120.0].note == "no darks at this epoch"


def test_warm_flats_excluded_nearest_session_chosen():
    lights = [_f("l1", night="2026-10-03"), _f("l2", night="2026-10-03")]
    cals = _many(20, kind="FLAT", exp=0.14, temp=38.7, session="2026-09-21") + \
        _many(20, kind="FLAT", exp=0.14, temp=0.0, session="2026-08-01") + \
        _many(20, kind="FLAT", exp=0.14, temp=0.0, session="2026-10-05")
    fc = calib.match_flats(cals, lights)["OSC"]
    assert fc.session == "2026-10-05" and len(fc.frames) == 20


def test_only_warm_flats_gives_none_with_reason():
    lights = [_f("l1")]
    cals = _many(20, kind="FLAT", exp=0.14, temp=38.7, session="2026-09-21")
    fc = calib.match_flats(cals, lights)["OSC"]
    assert not fc.frames and "uncooled" in fc.note


def test_flats_per_filter_mono():
    lights = [_f("h", filt="Ha", bayer="", inst="AP26MC"), _f("o", filt="OIII", bayer="", inst="AP26MC")]
    cals = _many(10, kind="FLAT", filt="Ha", inst="AP26MC", bayer="", session="2026-09-01")
    fl = calib.match_flats(cals, lights)
    assert fl["Ha"].frames and not fl["OIII"].frames


def test_plan_end_to_end_and_off_epoch_lights():
    lights = [_f(f"l{i}", exp=120.0) for i in range(5)] + [_f("odd", gain=200)]
    cals = (_many(50, kind="BIAS", exp=0.0, session="2026-09-12")
            + _many(50, kind="DARK", exp=120.0, session="2026-09-12"))
    p = calib.plan(lights, cals, default_readout="LCG")
    assert p.epoch.gain == 100 and len(p.bias) == 50
    assert p.dark_masters() == [120.0]
    assert any("several epochs" in n for n in p.notes)
    keep, off = calib.lights_in_epoch(lights, p.epoch, "LCG")
    assert len(keep) == 5 and off[0][0].name == "odd" and off[0][1] == "gain 200"


def test_plan_without_flats_option():
    p = calib.plan([_f("l")], [], use_flats=False)
    assert p.flats == {} and "flats: off (--no-flats)" in p.notes


def test_scan_reads_both_trees_and_skips_quarantine(tmp_path):
    lib = tmp_path / "Library"
    for rel in ("Calibration/DARK/2026-07-04/a.fits",
                "piggyback/Calibration/DARK/2026-09-12/b.fits",
                "piggyback/Calibration/BIAS/2026-09-12/c.fits",
                "piggyback/Calibration/_quarantine/DARK/2026-09-12/q.fits"):
        p = lib / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")

    def read(p, kind=None, tz=None):
        inst = "AP26MC" if "piggyback" not in str(p) else "AP26CC"
        return _f(Path(p).name, kind=kind, inst=inst)

    frames, skipped = calib.scan(lib, read=read, instrument="AP26CC")
    names = sorted(f.name for f in frames)
    assert names == ["b.fits", "c.fits"]
    assert {f.kind for f in frames} == {"DARK", "BIAS"}
    assert any("camera AP26MC" in why for _, why in skipped)
