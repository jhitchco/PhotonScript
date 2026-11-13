"""PS-137: Piggy-600 subs are named after the goal their own frame holds.

The 2026-09-21 case: 88 M31 Piggy subs, 29 recorded as "Crescent Nebula"
(the RC16's target) and 59 as "?", at two pointings about 51' apart with the
M31 core near the frame edge."""

import json
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
from astropy.io import fits

from photonscript.scheduler import piggy_attribution as pa
from photonscript.scheduler.runs import (
    _load_subs,
    append_sub_record,
    attribute_night,
    build_library,
    library_root,
)
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-09-21"
M31 = (10.6847, 41.2690)
# pointing A: M31 50' west of the frame centre (inside the 134' width);
# pointing B: M31 36' north of the centre, frame turned 90 deg. M 32 sits
# nearer the B centre but is an RC16-only goal: M31 (Piggy-driven) wins.
POINT_A = (10.6847 + 50 / 60 / 0.7516, 41.2690, 0.0)
POINT_B = (10.6847, 40.6690, 90.0)

GOALS = {
    "m31": {"target": {"name": "M31", "ra_hours": M31[0] / 15,
                       "dec_degrees": M31[1]},
            "driving_rig": "piggyback",
            "exposure_plans": [{"rig": "piggyback", "filter_type": "OSC"}]},
    "crescent": {"target": {"name": "Crescent Nebula", "ra_hours": 20.202,
                            "dec_degrees": 38.355},
                 "driving_rig": "rc16",
                 "exposure_plans": [{"rig": "rc16", "filter_type": "Ha"}]},
    "m32": {"target": {"name": "M 32", "ra_hours": 10.674 / 15,
                       "dec_degrees": 40.865},
            "driving_rig": "rc16",
            "exposure_plans": [{"rig": "rc16", "filter_type": "L"}]},
}


@pytest.fixture(autouse=True)
def _no_live_store(monkeypatch):
    """identify's header pass must not reach for the server's project store."""
    from photonscript.scheduler import identify
    monkeypatch.setattr(identify, "_candidates", lambda config: [])


def _config(tmp_path, mode="report"):
    (tmp_path / "projects.json").write_text(json.dumps(GOALS), encoding="utf-8")
    return PhotonScriptConfig(data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"),
                              piggyback_frame_attribution=mode,
                              stamp_fits_object=False)


def _fits(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    hdu = fits.PrimaryHDU(np.zeros((8, 8), dtype=np.uint16))
    hdu.header["IMAGETYP"] = "LIGHT"
    hdu.writeto(path, overwrite=True)
    return path


T0 = datetime(2026, 9, 22, 3, 0, 0)


def _piggy_night(config, n=88, n_crescent=29, files=False):
    """88 Piggy subs, 120 s each: the first 44 at pointing A, the rest at B.
    29 carry the RC16's name, 59 none. Returns {file: pointing}."""
    where = {}
    for i in range(n):
        rel = f"LIGHT/OSC_{i:04d}.fits"
        absp = str(config.image_watch_dir) + "/" + NIGHT + "/" + rel
        if files:
            _fits(Path(absp))
        append_sub_record(config, NIGHT, {
            "rig": "piggyback", "file": rel, "abs_path": absp,
            "time": (T0 + timedelta(seconds=121 * i)).isoformat(),
            "target": "Crescent Nebula" if i < n_crescent else "?",
            "filter": "OSC", "exp_s": 120, "passed_qa": True,
            "reviewed": True, "reason": ""})
        where[rel] = POINT_A if i < n // 2 else POINT_B
    return where


def _runner_for(where):
    """Fake ASTAP: the frame centre and rotation of the sub's pointing."""
    def runner(path, fov, hint, radius, timeout):
        name = str(path).replace("\\", "/").split("/")[-1]
        ra, dec, rot = where["LIGHT/" + name]
        return {"CRVAL1": ra, "CRVAL2": dec, "CDELT1": -1.29 / 3600,
                "CDELT2": 1.29 / 3600, "CROTA2": rot}
    return runner


# ------------------------------------------------------------------ geometry

def test_frame_contains_rectangle_and_rotation():
    w, h = 133.8, 89.6
    c = (10.0, 41.0)
    east_60 = (10.0 + 60 / 60 / 0.7547, 41.0)   # 60' east
    north_60 = (10.0, 42.0)                     # 60' north
    assert pa.frame_contains(*c, *east_60, w, h, pa_deg=0)      # in the width
    assert not pa.frame_contains(*c, *north_60, w, h, pa_deg=0)  # past 45'
    assert pa.frame_contains(*c, *north_60, w, h, pa_deg=90)     # turned
    assert not pa.frame_contains(*c, *east_60, w, h, pa_deg=90)
    # no rotation (a mount position): only half the short side counts
    assert not pa.frame_contains(*c, *east_60, w, h, pa_deg=None)
    assert pa.frame_contains(*c, 10.0, 41.7, w, h, pa_deg=None)  # 42'


def test_piggy_fov_from_config():
    w, h = pa.piggy_fov(PhotonScriptConfig())
    assert 133 < w < 135 and 89 < h < 91


def test_goal_tiers_and_preference(tmp_path):
    config = _config(tmp_path)
    cands = pa.goal_candidates(config)
    tiers = {c["name"]: c["tier"] for c in cands}
    assert tiers == {"M31": 0, "Crescent Nebula": 2, "M 32": 2}
    g = pa.choose_goal(*POINT_B, cands, 133.8, 89.6)
    assert g["name"] == "M31" and g["tier"] == 0     # M 32 is nearer
    # a passenger goal (Piggy plan, RC16 drives) beats an RC16-only goal
    goals = dict(GOALS)
    goals["m31"] = {**GOALS["m31"], "driving_rig": "rc16"}
    cands = pa.goal_candidates(config, list(goals.values()))
    assert {c["name"]: c["tier"] for c in cands}["M31"] == 1
    assert pa.choose_goal(*POINT_B, cands, 133.8, 89.6)["name"] == "M31"
    # nothing in the frame: no goal
    assert pa.choose_goal(150.0, 10.0, 0.0, cands, 133.8, 89.6) is None


# ------------------------------------------------------------------ 09-21

def test_0921_dry_run_reports_and_writes_nothing(tmp_path):
    config = _config(tmp_path)
    where = _piggy_night(config)
    before = (tmp_path / "runs" / f"{NIGHT}_subs.jsonl").read_text()
    r = pa.reattribute(config, [NIGHT], apply=False, solve=True,
                       runner=_runner_for(where), max_solves=100)
    assert r["applied"] is False and r["subs_changed"] == 88
    n = r["nights"][0]
    assert (n["piggy"], n["placed"], n["no_goal"]) == (88, 88, 0)
    ch = {(c["from"], c["to"]): c["subs"] for c in n["changes"]}
    assert ch == {("Crescent Nebula", "M31"): 29, ("?", "M31"): 59}
    assert (tmp_path / "runs" / f"{NIGHT}_subs.jsonl").read_text() == before


def test_0921_apply_renames_and_moves_library_links(tmp_path):
    config = _config(tmp_path)
    where = _piggy_night(config, files=True)
    # filed today: the 29 under Crescent, the 59 under Library/_
    lib = library_root(config)
    for s in _load_subs(config, NIGHT):
        folder = "Crescent Nebula" if s["target"] != "?" else "_"
        dest = lib / folder / "OSC" / s["file"].split("/")[-1]
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b"x")
    r = pa.reattribute(config, [NIGHT], apply=True, solve=True,
                       runner=_runner_for(where), max_solves=100)
    assert r["subs_changed"] == 88 and len(r["library_moves"]) == 88
    assert not r["library_collisions"]
    subs = _load_subs(config, NIGHT)
    assert {s["target"] for s in subs} == {"M31"}
    raw = [s["target_raw"] for s in subs]
    assert raw.count("Crescent Nebula") == 29 and raw.count("?") == 59
    assert all(s["target_src"] == "piggy-frame" for s in subs)
    a = subs[0]["target_attr"]
    assert a["name"] == "M31" and a["applied"] and a["src"] == "solve"
    assert a["tier"] == "piggy-driven" and 49 < a["off_arcmin"] < 51
    assert len(list((lib / "M31" / "OSC").iterdir())) == 88
    assert not list((lib / "Crescent Nebula" / "OSC").iterdir())
    # the solves were stored for reuse by the pointing pass
    from photonscript.scheduler import solve_store
    assert len(solve_store.lookup(config, NIGHT, "piggyback")) == 88
    # idempotent: a second run changes nothing
    r2 = pa.reattribute(config, [NIGHT], apply=True)
    assert r2["subs_changed"] == 0 and r2["nights"][0]["kept"] == 88


def test_library_collision_left_in_place(tmp_path):
    config = _config(tmp_path)
    where = _piggy_night(config, n=2, n_crescent=2)
    lib = library_root(config)
    for name in ("OSC_0000.fits", "OSC_0001.fits"):
        (lib / "Crescent Nebula" / "OSC").mkdir(parents=True, exist_ok=True)
        (lib / "Crescent Nebula" / "OSC" / name).write_bytes(b"old")
    (lib / "M31" / "OSC").mkdir(parents=True)
    (lib / "M31" / "OSC" / "OSC_0000.fits").write_bytes(b"there")
    r = pa.reattribute(config, [NIGHT], apply=True, solve=True,
                       runner=_runner_for(where))
    assert len(r["library_collisions"]) == 1 and len(r["library_moves"]) == 1
    assert (lib / "Crescent Nebula" / "OSC" / "OSC_0000.fits").read_bytes() == b"old"
    assert (lib / "M31" / "OSC" / "OSC_0000.fits").read_bytes() == b"there"


# ------------------------------------------------------------------ modes

def _store_solves(config, where, every=1):
    from photonscript.scheduler import solve_store
    p = solve_store.store_path(config, NIGHT, "piggyback")
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as fh:
        for i, (f, (ra, dec, rot)) in enumerate(sorted(where.items())):
            if i % every == 0:
                fh.write(json.dumps({"file": f, "rig": "piggyback",
                                     "solved": True, "ra": ra, "dec": dec,
                                     "pa": rot}) + "\n")


def test_report_mode_records_evidence_only(tmp_path):
    config = _config(tmp_path, mode="report")
    where = _piggy_night(config, n=4, n_crescent=2)
    _store_solves(config, where)
    attribute_night(config, NIGHT)
    subs = _load_subs(config, NIGHT)
    assert [s["target"] for s in subs[:2]] == ["Crescent Nebula"] * 2
    assert all(s["target_attr"]["name"] == "M31" for s in subs)
    assert not any(s["target_attr"]["applied"] for s in subs)
    assert not any(s.get("target_src") for s in subs)


def test_off_mode_does_nothing(tmp_path):
    config = _config(tmp_path, mode="off")
    where = _piggy_night(config, n=2, n_crescent=2)
    _store_solves(config, where)
    attribute_night(config, NIGHT)
    assert not any(s.get("target_attr") for s in _load_subs(config, NIGHT))


def test_on_mode_files_under_the_framed_goal(tmp_path):
    config = _config(tmp_path, mode="on")
    where = _piggy_night(config, n=4, n_crescent=2, files=True)
    _store_solves(config, where)
    res = build_library(config, NIGHT)     # runs attribute_night first
    assert res["attributed"] == 4
    lib = library_root(config)
    assert len(list((lib / "M31" / "OSC").iterdir())) == 4
    assert not (lib / "Crescent Nebula").exists()
    subs = _load_subs(config, NIGHT)
    assert [s["target_raw"] for s in subs] == ["Crescent Nebula"] * 2 + ["?"] * 2


def test_manual_assignment_and_rc16_subs_untouched(tmp_path):
    config = _config(tmp_path, mode="on")
    where = _piggy_night(config, n=2, n_crescent=2)
    append_sub_record(config, NIGHT, {
        "rig": "rc16", "file": "LIGHT/H_0000.fits", "time": T0.isoformat(),
        "target": "Crescent Nebula", "filter": "Ha", "exp_s": 600,
        "passed_qa": True})
    subs = _load_subs(config, NIGHT)
    subs[0]["target_src"] = "manual"
    from photonscript.scheduler.runs import _rewrite_subs
    _rewrite_subs(config, NIGHT, subs)
    _store_solves(config, where)
    attribute_night(config, NIGHT)
    by = {s["file"]: s for s in _load_subs(config, NIGHT)}
    assert by["LIGHT/OSC_0000.fits"]["target"] == "Crescent Nebula"
    assert by["LIGHT/OSC_0001.fits"]["target"] == "M31"
    assert by["LIGHT/H_0000.fits"]["target"] == "Crescent Nebula"
    assert "target_attr" not in by["LIGHT/H_0000.fits"]


def test_no_goal_in_frame_keeps_the_name(tmp_path):
    config = _config(tmp_path, mode="on")
    where = {f: (150.0, 10.0, 0.0)                 # a tracking-test field
             for f in _piggy_night(config, n=3, n_crescent=3)}
    _store_solves(config, where)
    r = pa.attribute_piggy_night(config, NIGHT)
    assert r["no_goal"] == 3 and r["renamed"] == 0
    assert {s["target"] for s in _load_subs(config, NIGHT)} == {"Crescent Nebula"}


def _rc16(config, i, ra, dec):
    append_sub_record(config, NIGHT, {
        "rig": "rc16", "file": f"LIGHT/R_{i:04d}.fits",
        "time": (T0 + timedelta(seconds=121 * i)).isoformat(), "ra": ra,
        "dec": dec, "target": "M 32", "filter": "L", "exp_s": 120,
        "passed_qa": True})


def test_neighbour_solve_borrowed_only_without_a_move(tmp_path):
    config = _config(tmp_path, mode="on")
    where = _piggy_night(config, n=6, n_crescent=6)
    # RC16 frames: steady for subs 0-2, then a 2 deg move before sub 3
    for i in range(6):
        _rc16(config, i, 10.68 if i < 3 else 12.68, 40.87)
    sol = {f: p for f, p in where.items() if f.endswith(("0000.fits", "0005.fits"))}
    _store_solves(config, sol)
    r = pa.attribute_piggy_night(config, NIGHT)
    by = {s["file"]: s for s in _load_subs(config, NIGHT)}
    # 1 borrows sub 0's solve and 4 sub 5's; 2 and 3 expose through the
    # RC16 move (the inferred window), so nothing crosses it
    assert by["LIGHT/OSC_0001.fits"]["target_attr"]["src"] == "solve (neighbour)"
    assert by["LIGHT/OSC_0004.fits"]["target_attr"]["src"] == "solve (neighbour)"
    assert "target_attr" not in by["LIGHT/OSC_0002.fits"]
    assert "target_attr" not in by["LIGHT/OSC_0003.fits"]
    assert r["placed"] == 4 and r["renamed"] == 4 and r["no_position"] == 2


def test_neighbour_solve_not_borrowed_across_a_move(tmp_path):
    config = _config(tmp_path, mode="on")
    where = _piggy_night(config, n=3, n_crescent=3)
    for i in range(3):
        _rc16(config, i, 10.68 if i < 1 else 12.68, 40.87)
    _store_solves(config, {"LIGHT/OSC_0000.fits": where["LIGHT/OSC_0000.fits"]})
    r = pa.attribute_piggy_night(config, NIGHT)
    assert r["placed"] == 1 and r["no_position"] == 2


def test_config_default_and_system_field():
    assert PhotonScriptConfig().piggyback_frame_attribution == "report"
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    f = by_env["PS_PIGGYBACK_FRAME_ATTRIBUTION"]
    assert f[0] == "piggyback_frame_attribution" and f[4] == "str"
    assert pa.mode(PhotonScriptConfig(piggyback_frame_attribution="bogus")) == "report"
