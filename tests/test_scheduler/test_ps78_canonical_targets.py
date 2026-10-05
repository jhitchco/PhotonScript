"""PS-78: subs named after NINA containers count for their real target."""

import json
from pathlib import Path

import pytest

from photonscript.scheduler import calibration as cal
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import runs
from photonscript.scheduler.target_backfill import rename_backfill
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget
from photonscript.shared.target_names import (
    canonical_target,
    is_container_name,
    known_target_index,
    target_key,
)
from photonscript.telescope_agent.nina_client import NINAAPI_CONTAINER_SUFFIX, _sequence_status

NIGHT = "2026-09-26"
HEART_C = "Heart Nebula imaging (repeats while safe and up)_Container"
CATS_C = "Cat's Eye Nebula imaging (repeats while safe and up)_Container"
OSC_C = "OSC_LIGHT_LOOP_Container"


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _write(cfg, rows, date=NIGHT):
    (runs.runs_dir(cfg) / f"{date}_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(target, time, *, rig="rc16", filt="Ha", ok=True, exp=300, name=None):
    name = name or f"{rig}_{time[11:19].replace(':', '-')}.fits"
    return {"rig": rig, "file": f"LIGHT/{name}",
            "abs_path": f"C:/NINA/{NIGHT}/LIGHT/{name}", "time": time,
            "target": target, "filter": filt, "exp_s": exp,
            "passed_qa": ok, "reviewed": ok, "reason": ""}


# --- canonical_target -------------------------------------------------------

@pytest.mark.parametrize("raw,want", [
    (HEART_C, "Heart Nebula"),
    (CATS_C, "Cat's Eye Nebula"),
    ("Heart Nebula imaging (repeats while safe and up)", "Heart Nebula"),
    ("heart nebula IMAGING (REPEATS WHILE SAFE AND UP)_container",
     "heart nebula"),
    ("Heart Nebula_Container", "Heart Nebula"),            # DSO container
    ("Heart Nebula focus calibration AFs_Container", "Heart Nebula"),
    (OSC_C, None),
    ("OSC_LIGHTS_UNTIL_DAWN_Container", None),
    ("OSC_IMAGE_PASS_Container", None),
    ("TARGETS_CONTAINER_Container", None),
    ("SAFE_LOOP_Container", None),
    ("Smart Exposure_Container", None),
    ("Ha until moonrise_Container", None),
    ("Targets_Container", None),
    ("?", None), ("", None), (None, None),
    ("Crescent Nebula", "Crescent Nebula"),                # plain names pass
    ("M 31", "M 31"),
])
def test_canonical_target(raw, want):
    assert canonical_target(raw) == want


def test_known_targets_fix_spelling_and_catalog_ids():
    proj = type("P", (), {"target": CelestialTarget(
        name="Cat's Eye Nebula", catalog_id="NGC 6543", ra_hours=17.9,
        dec_degrees=66.6)})()
    known = [proj, "Heart Nebula"]
    assert canonical_target("cats eye nebula imaging (repeats while safe "
                            "and up)_Container", known) == "Cat's Eye Nebula"
    assert canonical_target("NGC6543", known) == "Cat's Eye Nebula"
    assert canonical_target("heart NEBULA", known) == "Heart Nebula"
    assert canonical_target("Cat\u2019s Eye Nebula", {"x": "Cat's Eye Nebula"}) \
        == "Cat's Eye Nebula"
    assert canonical_target("Pacman Nebula", known) == "Pacman Nebula"
    idx = known_target_index(known)
    assert known_target_index(idx) is idx
    assert target_key("Cat's Eye Nebula") == target_key("CATS EYE NEBULA")
    assert is_container_name(HEART_C) and is_container_name(OSC_C)
    assert not is_container_name("Heart Nebula") and not is_container_name("?")


def _walk_lights(node, anc, out):
    """(container names from root to each light-taking item)"""
    if isinstance(node, dict):
        ty = str(node.get("$type", "")).split(",")[0].split(".")[-1]
        name = node.get("Name")
        light = ty == "SmartExposure" or (
            ty == "TakeExposure" and node.get("ImageType") == "LIGHT")
        chain = anc + [name] if name is not None and "Items" in node else anc
        if light:
            out.append(chain)
        for k, v in node.items():
            if k != "Parent":
                _walk_lights(v, chain, out)
    elif isinstance(node, list):
        for v in node:
            _walk_lights(v, anc, out)


def test_every_container_around_a_light_maps_to_its_target_or_none():
    """Guards future renames in the generator: whatever container NINA
    reports as running while a light is taken, it canonicalizes to the
    target or to 'unattributed', never to a bogus target name."""
    from tests.test_scheduler.test_ps77_safety_stop import _gen
    rc16 = _gen()
    piggy = json.loads(cal.generate_piggyback_companion_json(
        PhotonScriptConfig(_env_file=None), has_safety=True,
        with_lights=True))
    for seq, target in ((rc16, "Heart Nebula"), (piggy, None)):
        chains = []
        _walk_lights(seq, [], chains)
        assert chains
        for chain in chains:
            under = chain[chain.index("Targets") + 1:]
            assert under, chain
            for name in under:
                got = canonical_target(name + NINAAPI_CONTAINER_SUFFIX)
                assert got in (target, None), (name, got)
    # and the names actually come from the constants
    assert nsj.TARGET_IMAGING_SUFFIX in json.dumps(rc16)
    assert cal.OSC_LIGHT_LOOP_NAME in json.dumps(piggy)


def test_live_current_target_from_nina_tree_canonicalizes():
    tree = [{"Name": "Targets_Container", "Status": "RUNNING", "Items": [
        {"Name": "Heart Nebula_Container", "Status": "RUNNING", "Items": [
            {"Name": HEART_C, "Status": "RUNNING", "Items": [
                {"Name": "Smart Exposure", "Status": "RUNNING"}]}]}]}]
    raw = _sequence_status(tree)["CurrentTarget"]["Name"]
    assert raw == HEART_C
    from photonscript.telescope_agent.agent import TelescopeAgent
    assert TelescopeAgent._canonical_capture_name(raw) == "Heart Nebula"
    assert TelescopeAgent._canonical_capture_name(OSC_C) == ""
    assert TelescopeAgent._canonical_capture_name("Heart Nebula") \
        == "Heart Nebula"


# --- read side --------------------------------------------------------------

def test_resolve_target_strips_containers_and_falls_back_for_loops():
    plan = ["Cat's Eye Nebula", "Heart Nebula"]
    assert runs._resolve_target(HEART_C, "x.fits", plan) == "Heart Nebula"
    assert runs._resolve_target(OSC_C, "x.fits", plan) == "?"
    # one-target night: a loop name falls back like '?' always did
    assert runs._resolve_target(OSC_C, "x.fits", ["Heart Nebula"]) \
        == "Heart Nebula"
    assert runs._resolve_target("heart nebula", "x.fits", plan) \
        == "Heart Nebula"


def test_piggyback_correlation_uses_canonical_rc16_names():
    subs = [_row(HEART_C, "2026-09-27T02:00:00", exp=300),
            _row(OSC_C, "2026-09-27T02:02:00", rig="piggyback", filt="OSC"),
            _row(CATS_C, "2026-09-27T04:00:00", exp=60),
            _row(OSC_C, "2026-09-27T04:00:30", rig="piggyback", filt="OSC"),
            # an hour after the RC16 stopped: stays unattributed
            _row(OSC_C, "2026-09-27T06:00:00", rig="piggyback", filt="OSC")]
    n, windows, _ = runs.correlate_piggyback_records(subs)
    assert n == 2
    assert windows == {"Heart Nebula": 1, "Cat's Eye Nebula": 1}
    assert [s["target"] for s in subs] == [
        HEART_C, "Heart Nebula", CATS_C, "Cat's Eye Nebula", OSC_C]
    assert subs[1]["target_raw"] == OSC_C


def test_goal_sync_counts_container_named_subs(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.project_store import ProjectStore
    cfg = _cfg(tmp_path)
    store = ProjectStore(cfg)
    proj = store.add_from_target(CelestialTarget(
        name="Heart Nebula", catalog_id="IC 1805", ra_hours=2.55,
        dec_degrees=61.5, object_type="emission nebula"), budget_hours=5.0)
    store.update(proj.id, filter_mix={"Ha": 50, "OIII": 50})
    monkeypatch.setattr(app, "_store", store)
    # PS-118: subs at the plan's length, so seconds and counts agree
    oiii_s = next(e for e in proj.exposure_plans
                  if e.filter_type.value == "OIII").exposure_seconds
    _write(cfg, [_row(HEART_C, "2026-09-27T02:00:00", filt="OIII", exp=oiii_s),
                 _row(HEART_C, "2026-09-27T02:10:00", filt="OIII", exp=oiii_s),
                 _row("Heart Nebula", "2026-09-27T02:20:00", filt="OIII",
                      exp=oiii_s),
                 _row(HEART_C, "2026-09-27T02:30:00", filt="OIII", ok=False,
                      exp=oiii_s),
                 _row(OSC_C, "2026-09-27T02:31:00", rig="piggyback",
                      filt="OSC")])
    runs.sync_goal_progress(cfg)
    oiii = next(e for e in store.projects[proj.id].exposure_plans
                if e.filter_type.value == "OIII")
    assert oiii.acquired == 3
    # the live path: an accepted sub reported under the container name
    assert store.record_accepted_sub(HEART_C, "OIII", 300)
    assert not store.record_accepted_sub(OSC_C, "OSC", 120)
    # nights_by_target / night table group under the real name
    nbt = runs.nights_by_target(cfg)
    assert nbt["heart nebula"][0]["attempted"] == 4
    assert not any("container" in k for k in nbt)


def test_night_detail_groups_under_target_and_keeps_raw(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _write(cfg, [_row(HEART_C, "2026-09-27T02:00:00", filt="OIII"),
                 _row("Heart Nebula", "2026-09-27T02:20:00", filt="OIII")])
    monkeypatch.setattr(runs, "_cached_night_extras",
                        lambda c, d, n: ({"frames": {}}, type("R", (), {
                            "safe_hours": 0, "shutter_hours": 0,
                            "integrating_hours": 0, "sky_utilization_pct": 0,
                            "photon_efficiency_pct": 0})()))
    monkeypatch.setattr(runs, "calibration_inventory", lambda c, d: {})
    monkeypatch.setattr(runs, "_phase_stats", lambda c, d: {})
    d = runs.night_detail(cfg, NIGHT, backfill=False)
    rows = [r for r in d["table"] if r["rig"] == "rc16"]
    assert [(r["target"], r["attempted"]) for r in rows] == [
        ("Heart Nebula", 2)]
    assert d["subs"][0]["target_raw"] == HEART_C
    assert "target_raw" not in d["subs"][1]


def test_target_history_and_library_dirs(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", type("S", (), {"projects": {}})())
    _write(cfg, [_row(HEART_C, "2026-09-27T02:00:00", filt="OIII",
                      name="a.fits"),
                 _row(HEART_C, "2026-09-27T02:10:00", filt="OIII", ok=False,
                      name="b.fits"),
                 _row("Heart Nebula", "2026-09-27T02:20:00", filt="Ha",
                      name="c.fits"),
                 _row("Crescent Nebula", "2026-09-27T03:00:00", name="d.fits")])
    lib = runs.library_root(cfg)
    for folder, f in ((HEART_C, "a.fits"), ("Heart Nebula", "c.fits")):
        (lib / folder / "OIII").mkdir(parents=True, exist_ok=True)
        (lib / folder / "OIII" / f).write_bytes(b"x")
    dirs = runs.library_target_dirs(lib, "Heart Nebula")
    assert [d.name for d in dirs] == ["Heart Nebula", HEART_C]
    for name in ("Heart Nebula", HEART_C):
        h = app.api_target_history(name)  # PS-81: plain def
        assert h["target"] == "Heart Nebula"
        assert h["totals"]["accepted"] == 2 and h["totals"]["rejected"] == 1
        assert h["totals"]["in_library"] == 2
        assert {r["target_raw"] for r in h["nights"][0]["subs"]} == {
            HEART_C, "Heart Nebula"}


# --- backfill command ---------------------------------------------------------

def _seed_backfill(tmp_path):
    cfg = _cfg(tmp_path)
    (tmp_path / "projects.json").write_text(json.dumps({"p1": {"target": {
        "name": "Heart Nebula", "catalog_id": "IC 1805"}}}), encoding="utf-8")
    _write(cfg, [
        _row(HEART_C, "2026-09-27T02:00:00", filt="OIII", name="h1.fits"),
        _row(HEART_C, "2026-09-27T02:10:00", filt="OIII", name="h2.fits"),
        _row(OSC_C, "2026-09-27T02:01:00", rig="piggyback", filt="OSC",
             name="p1.fits"),
        _row(OSC_C, "2026-09-27T09:00:00", rig="piggyback", filt="OSC",
             name="p2.fits"),
        _row("Crescent Nebula", "2026-09-27T01:00:00", name="c1.fits")])
    lib = runs.library_root(cfg)
    files = {HEART_C: ["OIII/h1.fits", "OIII/h2.fits"],
             OSC_C: ["OSC/p1.fits", "OSC/p2.fits"],
             "Heart Nebula": ["OIII/h2.fits"],   # collision: already there
             "Crescent Nebula": ["Ha/c1.fits"]}
    for folder, rels in files.items():
        for rel in rels:
            p = lib / folder / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(folder.encode())
    rej = lib / "_rejected" / HEART_C / "OIII" / "h9.fits"
    rej.parent.mkdir(parents=True)
    rej.write_bytes(b"r")
    return cfg, lib


def _snapshot(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()}


def test_rename_backfill_dry_run_writes_nothing(tmp_path):
    cfg, _lib = _seed_backfill(tmp_path)
    before = _snapshot(tmp_path)
    r = rename_backfill(cfg)
    assert _snapshot(tmp_path) == before
    assert not r["applied"]
    assert r["subs_renamed"] == 4
    assert r["subs_by_target"] == {"Heart Nebula": 3}   # 2 RC16 + 1 piggy
    assert r["piggyback_still_unattributed"] == 1
    assert r["library_links_to_move"] == 4 and r["library_collisions"] == 1
    moves = {f["folder"]: f["to"] for f in r["library_folders"]}
    assert moves[HEART_C] == {"Heart Nebula": 1}
    assert moves[OSC_C] == {"Heart Nebula": 1, "_": 1}
    assert moves[str(Path("_rejected") / HEART_C)] == {
        str(Path("_rejected") / "Heart Nebula"): 1}


def test_rename_backfill_apply_moves_links_and_is_idempotent(tmp_path):
    cfg, lib = _seed_backfill(tmp_path)
    r = rename_backfill(cfg, apply=True)
    assert r["applied"] and r["library_collisions"] == 1
    subs = {s["file"]: s for s in runs._load_subs(cfg, NIGHT)}
    assert subs["LIGHT/h1.fits"]["target"] == "Heart Nebula"
    assert subs["LIGHT/h1.fits"]["target_raw"] == HEART_C
    assert subs["LIGHT/p1.fits"]["target"] == "Heart Nebula"
    assert subs["LIGHT/p2.fits"]["target"] == "?"
    assert subs["LIGHT/p2.fits"]["target_raw"] == OSC_C
    assert subs["LIGHT/c1.fits"]["target"] == "Crescent Nebula"
    assert "target_raw" not in subs["LIGHT/c1.fits"]
    assert (lib / "Heart Nebula" / "OIII" / "h1.fits").read_bytes() == \
        HEART_C.encode()
    assert (lib / "Heart Nebula" / "OSC" / "p1.fits").exists()
    assert (lib / "_" / "OSC" / "p2.fits").exists()
    assert (lib / "_rejected" / "Heart Nebula" / "OIII" / "h9.fits").exists()
    # the collision is left in place, the existing link untouched
    assert (lib / HEART_C / "OIII" / "h2.fits").exists()
    assert (lib / "Heart Nebula" / "OIII" / "h2.fits").read_bytes() == \
        b"Heart Nebula"
    assert not (lib / OSC_C).exists()
    assert (lib / "Crescent Nebula" / "Ha" / "c1.fits").exists()
    again = rename_backfill(cfg, apply=True)
    assert again["subs_renamed"] == 0
    assert again["library_links_to_move"] == 0
    assert again["library_collisions"] == 1


def test_rename_backfill_stamps_headers_only_when_asked(tmp_path):
    import numpy as np
    from astropy.io import fits
    cfg = _cfg(tmp_path)
    f = tmp_path / "fits" / NIGHT / "LIGHT" / "h1.fits"
    f.parent.mkdir(parents=True)
    hdu = fits.PrimaryHDU(np.zeros((4, 4), dtype=np.uint16))
    hdu.header["OBJECT"] = HEART_C[:68]
    hdu.writeto(f)
    row = _row(HEART_C[:68], "2026-09-27T02:00:00", name="h1.fits")
    row["abs_path"] = str(f)
    _write(cfg, [row])
    rename_backfill(cfg, apply=True)
    assert fits.getheader(f)["OBJECT"] == HEART_C[:68]
    _write(cfg, [row])
    r = rename_backfill(cfg, apply=True, stamp_headers=True)
    assert r["headers"]["stamped"] == 1
    assert fits.getheader(f)["OBJECT"] == "Heart Nebula"
