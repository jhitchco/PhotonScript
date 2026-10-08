"""PS-135: catalog aliases credit goals, the imported catalog rows are
audited, and the tracking-test picker sees the user catalog.

A sub named "NGC 224", "M31", "Messier 31" or "Andromeda" credits the
"Andromeda Galaxy" (M 31) goal: known_target_index() expands every known
target through the catalog alias resolver, and goal sync, the live credit
path, nights_by_target and the Targets page all build on it."""

import json

import pytest

from photonscript.scheduler import runs, sub_index
from photonscript.scheduler import tracking_test as tt
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared import astronomy, catalog_audit
from photonscript.shared.astronomy import catalog_alias_keys, find_catalog_entry
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget
from photonscript.shared.target_names import (canonical_target,
                                              known_target_index, target_key)

M31_NAMES = ["Andromeda Galaxy", "M 31", "M31", "m 31", "Messier 31",
             "NGC 224", "ngc224", "Andromeda"]


@pytest.fixture(autouse=True)
def no_user_catalog(monkeypatch):
    monkeypatch.setattr(astronomy, "_USER_TARGETS", [])


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _write(cfg, date, rows):
    (runs.runs_dir(cfg) / f"{date}_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _row(target, time, *, ok=True, exp=120, rig="piggyback", filt="OSC"):
    name = f"{rig}_{time[11:19].replace(':', '-')}.fits"
    return {"rig": rig, "file": f"LIGHT/{name}", "time": time,
            "target": target, "filter": filt, "exp_s": exp,
            "passed_qa": ok, "reviewed": ok, "reason": ""}


def _m31_store(cfg):
    store = ProjectStore(cfg)
    proj = store.add_from_target(CelestialTarget(
        name="Andromeda Galaxy", catalog_id="M 31", ra_hours=0.712,
        dec_degrees=41.27, object_type="galaxy"), budget_hours=6.0)
    store.update(proj.id, osc_hours=6.0, drop_rc16=True,
                 driving_rig="piggyback")
    return store, proj


# ---------------------------------------------------------------- resolver

@pytest.mark.parametrize("name", M31_NAMES)
def test_every_m31_spelling_gives_the_same_alias_set(name):
    assert catalog_alias_keys(name) == catalog_alias_keys("M 31")
    assert {"m31", "ngc224", "andromeda"} <= catalog_alias_keys(name)


@pytest.mark.parametrize("a,b", [
    ("M 42", "NGC 1976"), ("Orion", "Orion Nebula"), ("M 76", "NGC 650"),
    ("Barbell Nebula", "Little Dumbbell"), ("M 101", "Pinwheel Galaxy"),
    ("M 1", "Crab"), ("IC 1805", "Heart"), ("Messier 51", "NGC 5194"),
])
def test_cross_ids_and_common_names(a, b):
    assert target_key(b) in catalog_alias_keys(a)


@pytest.mark.parametrize("name", [
    "Veil Nebula",   # Western and Eastern both: ambiguous, so not an alias
    "Eyes",          # NGC 4435 and NGC 4438
    "Owl",           # Owl Nebula and Owl Cluster
    "bogus target", "", None])
def test_ambiguous_or_unknown_names_resolve_to_nothing(name):
    assert catalog_alias_keys(name) == frozenset()


def test_messier_without_a_catalog_row_still_cross_ids():
    assert catalog_alias_keys("M 40") == {"m40", "messier40"}


def test_known_index_maps_aliases_to_the_project_name():
    store_like = [CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                                  ra_hours=0.712, dec_degrees=41.27)]
    known = known_target_index(store_like)
    for n in M31_NAMES:
        assert canonical_target(n, known) == "Andromeda Galaxy"
    # a non-catalog name still passes through
    assert canonical_target("Tracking test X", known) == "Tracking test X"


def test_a_given_name_beats_another_targets_catalog_alias():
    # a project literally named "Andromeda" is not swallowed by M 31
    known = known_target_index(["Andromeda Galaxy", "Andromeda"])
    assert canonical_target("Andromeda", known) == "Andromeda"
    assert canonical_target("NGC 224", known) == "Andromeda Galaxy"


def test_user_catalog_aliases(monkeypatch):
    monkeypatch.setattr(astronomy, "_USER_TARGETS", [
        {"name": "My Blob", "catalog_id": "LDN 1251", "ra": 22.6, "dec": 75.2,
         "months": [9], "hours": 10, "aliases": ["Space Shark"]}])
    known = known_target_index(["My Blob"])
    assert canonical_target("LDN1251", known) == "My Blob"
    assert canonical_target("space shark", known) == "My Blob"


# ------------------------------------------------------------- crediting

def test_goal_sync_live_credit_and_nights_resolve_aliases(tmp_path):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store, proj = _m31_store(cfg)
    _write(cfg, "2026-10-03", [
        _row("NGC 224", "2026-10-04T01:00:00", exp=300),
        _row("Andromeda", "2026-10-04T01:06:00", exp=400),
        _row("Messier 31", "2026-10-04T01:13:00", exp=400, ok=False)])
    old = app._store
    app._store = store
    try:
        runs.sync_goal_progress(cfg)
    finally:
        app._store = old
    osc = store.projects[proj.id].exposure_plans[0]
    assert osc.rig == "piggyback" and osc.acquired_s == 300 + 400
    # the live path (record_accepted_sub) takes the same spellings
    assert store.record_accepted_sub("NGC 224", "OSC", 120, rig="piggyback")
    assert store.projects[proj.id].exposure_plans[0].acquired_s == 820
    assert not store.record_accepted_sub("NGC 891", "OSC", 120,
                                         rig="piggyback")
    nbt = runs.nights_by_target(cfg, store.projects.values())
    assert nbt == {"andromeda galaxy": [
        {"date": "2026-10-03", "accepted": 2, "attempted": 3,
         # PS-63: the per-rig split of the same night
         "by_rig": {"piggyback": {"accepted": 2, "attempted": 3}}}]}


def test_targets_page_groups_alias_subs_under_the_goal(tmp_path):
    cfg = _cfg(tmp_path)
    store, _ = _m31_store(cfg)
    _write(cfg, "2026-10-03", [
        _row("NGC 224", "2026-10-04T01:00:00"),
        _row("M31", "2026-10-04T01:06:00"),
        _row("Andromeda Galaxy", "2026-10-04T01:13:00")])
    projects = list(store.projects.values())
    cards = [c for c in sub_index.targets(cfg, projects)
             if not c["unattributed"] and c["counts"]["total"]]
    assert [(c["name"], c["counts"]["total"]) for c in cards] == [
        ("Andromeda Galaxy", 3)]
    assert len(sub_index.rows(cfg, projects, target="NGC 224")) == 3


def test_unknown_name_still_falls_back_to_ra_dec(tmp_path):
    """PS-51 is untouched: a name neither a project nor the catalog knows
    stays as-is for the RA/Dec attribution to handle."""
    known = known_target_index(["Andromeda Galaxy"])
    assert canonical_target("Field 7", known) == "Field 7"


# ------------------------------------------------------------- catalog audit

# Findings left for Jeremy (see the PS-135 build notes); anything new here
# is a new suspicious row.
KNOWN_FINDINGS = {
    ("PGC088608", "faint_large"),   # Sextans dSph really is V ~10.4
}


def test_catalog_audit_has_only_the_known_findings():
    got = {(f["catalog_id"], f["check"]) for f in catalog_audit.audit()}
    assert got == KNOWN_FINDINGS


@pytest.mark.parametrize("row,check", [
    ({"name": "X", "catalog_id": "M 77", "ra": 2.71, "dec": 0.013,
      "months": [9, 10, 11, 12]}, "dec_sign"),
    ({"name": "X", "catalog_id": "NGC 9999", "ra": 2.71, "dec": -0.5,
      "months": [9, 10, 11, 12]}, "dec_sign"),
    ({"name": "X", "catalog_id": "M 76", "ra": 1.70, "dec": 51.6, "size": 1.1,
      "months": [9, 10, 11, 12]}, "size"),
    ({"name": "X", "catalog_id": "NGC 9999", "ra": 1.70, "dec": 51.6,
      "months": [4, 5]}, "months"),
    ({"name": "the X", "catalog_id": "NGC 9999", "ra": 1.70, "dec": 51.6,
      "months": [9, 10]}, "odd_name"),
    ({"name": "X", "catalog_id": "NGC 9999", "ra": 25.0, "dec": 51.6},
     "range"),
])
def test_audit_flags_suspicious_rows(row, check):
    assert [f["check"] for f in catalog_audit.audit([row])] == [check]


def test_audit_flags_duplicate_names_and_ids():
    rows = [{"name": "Eyes", "catalog_id": "NGC 4435", "ra": 12.46,
             "dec": 13.0, "months": [3]},
            {"name": "Eyes", "catalog_id": "NGC 4435", "ra": 12.46,
             "dec": 13.0, "months": [3]}]
    assert [f["check"] for f in catalog_audit.audit(rows)] == [
        "dup_name", "dup_id"]


def test_fixed_rows():
    m76 = find_catalog_entry("M 76")
    assert m76["name"] == "Little Dumbbell Nebula" and m76["size"] == 2.7
    assert find_catalog_entry("Barbell Nebula")["catalog_id"] == "M 76"
    assert find_catalog_entry("NGC 253")["name"] == "Sculptor Galaxy"
    assert find_catalog_entry("IC 434")["name"] == "IC 434"  # not the Flame
    assert find_catalog_entry("M 11")["name"] == "Wild Duck Cluster"
    for cid, sign in catalog_audit.NEAR_EQUATOR_SIGN.items():
        assert (find_catalog_entry(cid)["dec"] > 0) == (sign > 0), cid


# ------------------------------------------------- rows follow-up (PS-135b)

def test_flame_nebula_row():
    for n in ("Flame Nebula", "Flame", "NGC 2024", "ngc2024"):
        e = find_catalog_entry(n)
        assert e["catalog_id"] == "NGC 2024", n
    e = find_catalog_entry("Flame Nebula")
    assert e["type"] == "emission nebula" and e["size"] == 30.0
    assert e["dec"] < 0 and abs(e["ra"] - 5.698) < 0.01
    assert set(e["months"]) == set(astronomy.months_for_ra(e["ra"]))
    assert e["mix"]["Ha"] == max(e["mix"].values())
    assert find_catalog_entry("IC 434")["catalog_id"] == "IC 434"


@pytest.mark.parametrize("old,cid", [
    ("Eagle Nebula", "M 16"), ("IC 4703", "M 16"),
    ("Eastern Veil", "NGC 6992"), ("NGC 6995", "NGC 6992"),
    ("Bear Claw Nebula", "NGC 2537"), ("Bear Paw Galaxy", "NGC 2537"),
    ("Browning", "IC 2431"), ("Cocoon Galaxy", "NGC 4490"),
    ("NGC 4990", "NGC 4990"),
])
def test_renamed_rows_keep_old_names(old, cid):
    assert find_catalog_entry(old)["catalog_id"] == cid


def test_duplicates_fold_into_one_target():
    ids = [e["catalog_id"] for e in astronomy.SEASONAL_TARGETS]
    assert "IC 4703" not in ids  # the M 16 row covers it
    assert catalog_alias_keys("IC 4703") == catalog_alias_keys("M 16")
    assert catalog_alias_keys("NGC 6995") == catalog_alias_keys("NGC 6992")
    assert catalog_alias_keys("Eastern Veil") == catalog_alias_keys("NGC 6992")
    known = known_target_index([CelestialTarget(
        name="Veil Nebula (Eastern)", catalog_id="NGC 6992", ra_hours=20.94,
        dec_degrees=31.72)])
    for n in ("NGC 6995", "Eastern Veil", "NGC6992"):
        assert canonical_target(n, known) == "Veil Nebula (Eastern)"
    known = known_target_index([CelestialTarget(
        name="Eagle Nebula (Pillars of Creation)", catalog_id="M 16",
        ra_hours=18.313, dec_degrees=-13.79)])
    for n in ("IC 4703", "Eagle Nebula", "NGC 6611", "M16"):
        assert canonical_target(n, known) == "Eagle Nebula (Pillars of Creation)"


def test_renamed_row_values():
    assert find_catalog_entry("NGC 2537")["name"] == "Bear Paw Galaxy"
    assert find_catalog_entry("IC 2431")["name"] == "IC 2431"
    assert find_catalog_entry("NGC 4990")["name"] == "NGC 4990"
    cocoon = find_catalog_entry("NGC 4490")
    assert cocoon["name"] == "Cocoon Galaxy"
    assert abs(cocoon["ra"] - 12.51) < 0.01 and abs(cocoon["dec"] - 41.64) < 0.01
    assert find_catalog_entry("NGC 6995")["catalog_id"] == "NGC 6992"
    part = find_catalog_entry("NGC 6995 (Eastern Veil, part)")
    assert part["catalog_id"] == "NGC 6995"
    assert find_catalog_entry("NGC 4945")["mag"] == 9.3


def test_check_script_runs(capsys):
    import importlib.util
    from pathlib import Path
    p = Path(__file__).resolve().parents[2] / "scripts" / "check_catalog.py"
    spec = importlib.util.spec_from_file_location("check_catalog", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    import sys
    old = sys.argv
    sys.argv = ["check_catalog.py"]
    try:
        assert mod.main() == 1  # the known finding
    finally:
        sys.argv = old
    assert "PGC088608" in capsys.readouterr().out


# ------------------------------------------------------- tracking-test picker

def test_tracking_picker_sees_the_user_catalog(monkeypatch):
    monkeypatch.setattr(astronomy, "_USER_TARGETS", [
        {"name": "My Field", "catalog_id": "", "ra": 3.0, "dec": 40.0,
         "type": "custom", "months": [10], "hours": 10, "user": True}])
    c = tt._candidates()
    mine = [x for x in c if x["name"] == "My Field"]
    assert mine == [{"name": "My Field", "ra_hours": 3.0, "dec_degrees": 40.0,
                     "type": "custom", "source": "catalog"}]
    # built-in rows are still there, once each
    names = [x["name"] for x in c]
    assert "Heart Nebula" in names and len(names) == len(set(
        n.lower() for n in names))
