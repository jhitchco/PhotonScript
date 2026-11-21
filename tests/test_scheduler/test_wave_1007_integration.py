"""wave-2026-10-07 integration: the seams between branches merged together.

- PS-137 frame attribution matches goal names through the PS-135 alias index
  and keeps PS-111's panel -> companion crediting.
- PS-48 per-target QA gates reach a sub named by a PS-135 catalog alias.
- PS-117 light-budget fields and PS-134 rc16_hours in one PATCH; an OSC
  resize keeps the plan's own sub length (the PS-117 M31 300 s decision).
- The PS-117 M31 migration never picks a PS-111 mosaic panel."""

import json

from photonscript.scheduler import piggy_attribution as pa
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared import qa_rules
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget

M31 = (10.6847, 41.2690)
M33 = (23.4621, 30.6599)


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _goal(pid, name, ra_deg, dec, rig="rc16", cid="", mosaic=None):
    plans = [{"rig": rig, "filter_type": "OSC" if rig != "rc16" else "L"}]
    return {"id": pid, "target": {"name": name, "catalog_id": cid,
                                  "ra_hours": ra_deg / 15,
                                  "dec_degrees": dec},
            "driving_rig": rig, "exposure_plans": plans, "mosaic": mosaic}


GOALS = [
    _goal("m31", "Andromeda Galaxy", *M31, rig="piggyback", cid="M 31"),
    _goal("p1", "M31 Core P1", *M31,
          mosaic={"id": "mos1", "name": "M31 Core", "panel": 1,
                  "companion": "m31"}),
    _goal("m33", "Triangulum Galaxy", *M33, rig="piggyback", cid="M 33"),
]


def _run(monkeypatch, tmp_path, target, at):
    monkeypatch.setattr(pa, "positions", lambda *a, **k: {
        "f.fits": {"ra": at[0], "dec": at[1], "pa": 0.0, "src": "solve"}})
    sub = {"file": "f.fits", "rig": "piggyback", "target": target,
           "filter": "OSC"}
    res = pa.attribute_records(_cfg(tmp_path), "2026-10-07", [sub],
                               apply=True, projects=GOALS)
    return sub, res


def test_ps137_alias_spelling_counts_as_the_goal(monkeypatch, tmp_path):
    sub, res = _run(monkeypatch, tmp_path, "NGC 224", M31)
    assert res["kept"] == 1 and not res["changed"]
    assert sub["target"] == "NGC 224"
    assert sub["target_attr"]["name"] == "Andromeda Galaxy"


def test_ps137_panels_are_never_candidates(tmp_path):
    names = {c["name"] for c in pa.goal_candidates(_cfg(tmp_path), GOALS)}
    assert "M31 Core P1" not in names and "Andromeda Galaxy" in names


def test_ps137_panel_sub_keeps_panel_name_and_credits_companion(
        monkeypatch, tmp_path):
    sub, res = _run(monkeypatch, tmp_path, "M31 Core P1", M31)
    assert res["kept"] == 1 and not res["changed"]
    assert sub["target"] == "M31 Core P1"
    assert sub["target_attr"]["tier"] == "mosaic companion"
    assert sub["target_attr"]["name"] == "Andromeda Galaxy"


def test_ps137_wins_over_panel_for_another_piggy_driven_goal(
        monkeypatch, tmp_path):
    sub, res = _run(monkeypatch, tmp_path, "M31 Core P1", M33)
    assert [c["to"] for c in res["changed"]] == ["Triangulum Galaxy"]
    assert sub["target"] == "Triangulum Galaxy"
    assert sub["target_raw"] == "M31 Core P1"


def test_ps48_override_reaches_catalog_alias(tmp_path):
    raw = {"m31": {"target": {"name": "Andromeda Galaxy",
                              "catalog_id": "M 31"},
                   "qa_overrides": {"piggyback": {"hfr_max": 7.5}}}}
    (tmp_path / "projects.json").write_text(json.dumps(raw),
                                            encoding="utf-8")
    cfg = _cfg(tmp_path)
    for name in ("Andromeda Galaxy", "M31", "NGC 224"):
        hit = qa_rules.goal_override(cfg, "piggyback", name)
        assert hit == ("Andromeda Galaxy", {"hfr_max": 7.5}), name


def _m31_store(tmp_path):
    store = ProjectStore(_cfg(tmp_path))
    t = CelestialTarget(name="Andromeda Galaxy", catalog_id="M 31",
                        ra_hours=0.712, dec_degrees=41.27,
                        object_type="galaxy")
    p = store.add_from_target(t, budget_hours=10)
    store.update(p.id, osc_hours=20, drop_rc16=True,
                 driving_rig="piggyback")
    return store, p


def test_light_budget_and_rc16_hours_in_one_update(tmp_path):
    store, p = _m31_store(tmp_path)
    store.update(p.id, rc16_hours=8,
                 light_budget={"goal_snr": 25, "feature_note": "outer disk"})
    assert p.goal_snr == 25.0 and p.feature_note == "outer disk"
    assert {e.rig for e in p.exposure_plans} == {"rc16", "piggyback"}


def test_osc_resize_keeps_the_plan_sub_length(tmp_path):
    store, p = _m31_store(tmp_path)
    osc = next(e for e in p.exposure_plans if e.rig == "piggyback")
    osc.exposure_seconds = 300.0          # the PS-117 M31 decision
    store.update(p.id, osc_hours=20)
    osc = next(e for e in p.exposure_plans if e.rig == "piggyback")
    assert osc.exposure_seconds == 300.0 and osc.count == 240


def test_ps117_m31_migration_skips_mosaic_panels(tmp_path):
    from photonscript.shared.models import ImagingProject
    store, p = _m31_store(tmp_path)
    panel = ImagingProject(
        id="panel", target=CelestialTarget(
            name="M31 Core P1", catalog_id="M 31", ra_hours=0.712,
            dec_degrees=41.27, object_type="galaxy"),
        mosaic={"id": "mos1", "panel": 1, "companion": p.id})
    store.projects = {"panel": panel, p.id: p}
    (tmp_path / store.MIGRATIONS_FILE).write_text(
        json.dumps({"ps30_m31_osc": True}), encoding="utf-8")
    store._migrate_m31_light_budget()
    assert p.goal_snr == 20.0 and panel.goal_snr is None
    osc = next(e for e in p.exposure_plans if e.rig == "piggyback")
    assert osc.exposure_seconds == 300.0
