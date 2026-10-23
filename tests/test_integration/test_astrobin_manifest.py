"""PS-22: AstroBin CSV + packet text, and the run manifest."""
from pathlib import Path

import pytest

from photonscript.integration import astrobin, manifest
from photonscript.integration.frames import Frame


def _l(name, exp, night, loc, foc=21.3):
    return Frame(path=Path(name), exp=exp, gain=100, offset=256, set_temp=0.0, xbin=1,
                 focratio=5.6, filter="OSC", bayer="RGGB", night=night, date_loc=loc,
                 date_obs=loc, foctemp=foc, instrument="AP26CC")


def _m31_frames():
    fs = [_l(f"a{i}", 120.0, "2026-09-20", f"2026-09-21T03:{i:02d}:00") for i in range(47)]
    fs += [_l(f"b{i}", 300.0, "2026-10-03", f"2026-10-03T20:0{i}:00") for i in range(2)]
    fs += [_l(f"c{i}", 400.0, "2026-10-03", f"2026-10-03T21:{i:02d}:00") for i in range(69)]
    return fs


def test_acquisition_rows_match_the_m31_v4b_packet():
    rows = astrobin.acquisition_rows(_m31_frames(), darks_for={120.0: 50, 300.0: 50, 400.0: 50},
                                     flats_for={"OSC": 0}, bias=50)
    txt = astrobin.csv_text(rows)
    assert txt.splitlines() == [
        "date,number,duration,binning,gain,sensorCooling,fNumber,darks,flats,flatDarks,bias,bortle,temperature",
        "2026-09-21,47,120,1,100,0,5.6,50,0,0,50,2,21.3",
        "2026-10-03,2,300,1,100,0,5.6,50,0,0,50,2,21.3",
        "2026-10-03,69,400,1,100,0,5.6,50,0,0,50,2,21.3",
    ]


def test_temperature_is_median_foctemp_per_night():
    fs = [_l("x1", 120.0, "n1", "2026-01-01T20:00:00", 10.0),
          _l("x2", 120.0, "n1", "2026-01-01T21:00:00", 12.0),
          _l("x3", 120.0, "n1", "2026-01-01T22:00:00", 30.0)]
    rows = astrobin.acquisition_rows(fs, darks_for={}, flats_for={}, bias=0)
    assert rows[0]["temperature"] == "12" and rows[0]["darks"] == 0


def test_fmt_duration():
    assert astrobin.fmt_duration(33840) == "9 h 24 m"
    assert astrobin.fmt_duration(2520) == "42 m"


def test_no_em_dash_guard():
    with pytest.raises(ValueError):
        astrobin.no_em_dash("a " + chr(0x2014) + " b")
    with pytest.raises(ValueError):
        astrobin.no_em_dash("caf" + chr(0xE9))
    assert astrobin.no_em_dash("plain, ascii: ok") == "plain, ascii: ok"


def _info(**kw):
    rows = astrobin.acquisition_rows(_m31_frames(), darks_for={120.0: 50, 300.0: 50, 400.0: 50},
                                     flats_for={"OSC": 0}, bias=50)
    info = {"target": "Andromeda Galaxy", "run_name": "Andromeda_Galaxy_piggyback_20261006-0400",
            "run_dir": "D:/S/run", "built": "2026-10-06 04:00", "rows": rows, "integrated": 118,
            "total_s": 33840.0, "staged": 159,
            "qa": {"mode": "apply", "kept": 106, "rejected": 53,
                   "reasons": [("second_star_set", 41), ("trailed", 9)]},
            "calibration": {"bias": 50, "darks": [(120.0, 50, 120.0, False), (400.0, 50, 120.0, True)],
                            "flats": [("OSC", 0, "no matching flats")], "notes": ["bias: 50 frames"]},
            "equipment": {"camera": "AP26CC", "optics": "600 mm f/5.6", "mount": "TheSky",
                          "software": "N.I.N.A. 3.2"},
            "location": {"name": "AARO", "lat": 31.907, "lon": -109.021, "elev": 1300, "bortle": 2},
            "timing": [("star QA", 1.0)], "integrated_by_pi": True, "final": "x_final.jpg",
            "processing": "chain.", "known_issues": ["No flats."]}
    info.update(kw)
    return info


def test_packet_md_content_and_ascii():
    md = astrobin.packet_md(_info())
    md.encode("ascii")
    assert chr(0x2014) not in md
    assert "**Andromeda Galaxy (WIP)**" in md
    assert "118 subs, 9 h 24 m in total over 2 nights" in md
    assert "50 x 120 s darks for the 400 s subs (scaled with dark optimization)" in md
    assert "no flats yet" in md
    assert "31.907 N, 109.021 W, 1300 m, Bortle 2" in md
    assert "Never press Publish" in md
    assert "| second_star_set | 41 |" in md
    assert "2026-09-21,47,120" in md


def test_packet_md_planned_only_says_so():
    md = astrobin.packet_md(_info(integrated_by_pi=False, final=""))
    assert "PLANNED only" in md and "Not made" in md


def test_manifest_build_write_read(tmp_path):
    fs = _m31_frames()[:3]
    for f in fs:
        f.size = 10
    entries = [manifest.frame_entry(fs[0], "light", "OSC 120s", "D:/S/a0", {"action": "keep", "reason": "stars_ok"}),
               manifest.frame_entry(fs[1], "light", "OSC 120s", "", {"action": "reject", "reason": "trailed"}),
               manifest.frame_entry(fs[2], "dark", "DARK 120s", "D:/S/d")]
    m = manifest.build({"target": "M31", "rig": "piggyback"}, entries, {"bias": 0}, {"mode": "apply"})
    assert m["counts"]["lights_selected"] == 2 and m["counts"]["lights_staged"] == 1
    assert m["counts"]["darks"] == 1 and m["integration_s"] == 120.0
    jp, cp = manifest.write(tmp_path, m)
    assert manifest.read(tmp_path)["target"] == "M31"
    lines = cp.read_text().splitlines()
    assert lines[0].startswith("group,role,file,source,staged,bytes")
    assert len(lines) == 4 and "reject" in lines[2]
