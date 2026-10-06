"""PS-129: goal cards show the plans a goal actually has, and the Nights list
names every night that credited the goal.

M31 (Andromeda Galaxy, catalog "M 31") is a Piggy-600 only goal. Its
2026-10-03 subs were recorded as "M 31": the goal sync credited them (it
matches project names and catalog ids) but nights_by_target matched only the
night plan's names, so the card listed just 2026-09-20."""

import json
from pathlib import Path

from photonscript.scheduler import runs
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget


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


def _seed_m31_nights(cfg):
    _write(cfg, "2026-09-20", [
        _row("Andromeda Galaxy", "2026-09-21T01:00:00"),
        _row("Andromeda Galaxy", "2026-09-21T01:02:00", ok=False)])
    _write(cfg, "2026-10-03", [
        _row("M 31", "2026-10-04T01:00:00", exp=300),
        _row("M 31", "2026-10-04T01:06:00", exp=400),
        _row("M 31", "2026-10-04T01:13:00", exp=400, ok=False)])


def test_nights_by_target_lists_every_night_that_credited_the_goal(tmp_path):
    cfg = _cfg(tmp_path)
    store, proj = _m31_store(cfg)
    _seed_m31_nights(cfg)
    # the goal sync credits both nights (catalog id "M 31")
    from photonscript.scheduler import app
    app_store = app._store
    app._store = store
    try:
        runs.sync_goal_progress(cfg)
    finally:
        app._store = app_store
    osc = store.projects[proj.id].exposure_plans[0]
    assert osc.rig == "piggyback" and osc.acquired_s == 120 + 300 + 400
    # ...and the Nights list now names both of them under the goal
    nbt = runs.nights_by_target(cfg, store.projects.values())
    assert nbt["andromeda galaxy"] == [
        {"date": "2026-10-03", "accepted": 2, "attempted": 3},
        {"date": "2026-09-20", "accepted": 1, "attempted": 2}]
    assert "m 31" not in nbt


def test_nights_by_target_without_projects_keeps_raw_names(tmp_path):
    """No projects (old call shape): names are only canonicalized."""
    cfg = _cfg(tmp_path)
    _seed_m31_nights(cfg)
    nbt = runs.nights_by_target(cfg)
    assert [n["date"] for n in nbt["andromeda galaxy"]] == ["2026-09-20"]
    assert [n["date"] for n in nbt["m 31"]] == ["2026-10-03"]


def test_nights_by_target_project_spelling_wins_over_plan_name(tmp_path):
    cfg = _cfg(tmp_path)
    store, _ = _m31_store(cfg)
    (runs.runs_dir(cfg) / "2026-10-03_plan.json").write_text(
        json.dumps({"targets": [{"name": "M 31"}]}), encoding="utf-8")
    _write(cfg, "2026-10-03", [_row("?", "2026-10-04T01:00:00"),
                               _row("M31", "2026-10-04T01:05:00")])
    nbt = runs.nights_by_target(cfg, store.projects.values())
    # the '?' sub falls back to the night's only plan target, which then
    # resolves to the project like the named one
    assert nbt == {"andromeda galaxy": [
        {"date": "2026-10-03", "accepted": 2, "attempted": 2}]}


def test_api_projects2_nights_use_goal_matching(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store, _ = _m31_store(cfg)
    _seed_m31_nights(cfg)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", store)
    out = app.api_projects2()
    m31 = next(d for d in out if d["target"]["name"] == "Andromeda Galaxy")
    assert [n["date"] for n in m31["nights"]] == ["2026-10-03", "2026-09-20"]
    assert [e["rig"] for e in m31["exposure_plans"]] == ["piggyback"]


def _template() -> str:
    p = (Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
         / "templates" / "dashboard.html")
    return p.read_text(encoding="utf-8")


def test_goal_card_renders_rows_per_plan_present():
    """Static checks on the card renderer: RC16 mix rows only when the goal
    has an RC16 plan, one row per Piggy-600 plan, shared % rule. The PS-129
    code is ASCII."""
    s = _template()
    i = s.index("PS-129: rows per plan actually present")
    block = s[i:s.index("filterRows + oscRows +", i)]
    # the new code is ASCII (the RC16 row markup between keeps its glyphs)
    block[:block.index("const e = plansBy[f];")].encode("ascii")
    o = block.index("PS-129: one row per Piggy-600 plan")
    block[o:block.index("}).join('');", o)].encode("ascii")
    assert "e.rig === 'piggyback'" in block and "e.rig !== 'piggyback'" in block
    assert "const showMix = rc16Plans.length > 0 || !piggyPlans.length;" in block
    assert "rc16Plans.forEach(e => { plansBy[e.filter_type] = e; });" in block
    assert "const filterRows = !showMix ? '' : ALL_FILTERS.map(" in block
    assert "const oscRows = piggyPlans.map(" in block
    assert block.count("planPct(e)") == 2
    # the mix buttons only for goals with RC16 rows
    k = s.index("filterRows + oscRows +")
    tail = s[k:s.index("accepted lights in library", k)]
    assert "(showMix" in tail and "Piggy-600 only goal (no RC16 plan)" in tail
    j = s.index("function planPct(e)")
    pp = s[j:s.index("\n    }\n", j)]
    pp.encode("ascii")
    assert "e.acquired_s" in pp and "e.count * e.exposure_seconds" in pp
