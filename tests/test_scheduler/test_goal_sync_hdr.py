"""sync_goal_progress splits HDR short subs from the long set by length."""
import json

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget
from photonscript.scheduler import runs


def test_sync_goal_progress_splits_short_and_long(tmp_path, monkeypatch):
    from photonscript.scheduler.project_store import ProjectStore
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path)
    store = ProjectStore(cfg)
    t = CelestialTarget(name="HDR Sync", catalog_id="NGC 9998",
                        ra_hours=1.0, dec_degrees=40.0,
                        object_type="planetary nebula")
    proj = store.add_from_target(t, budget_hours=5.0)
    store.update(proj.id, filter_mix={"Ha": 100}, hdr={"Ha": 60})
    import photonscript.scheduler.app as app
    monkeypatch.setattr(app, "_store", store)
    rows = ([{"target": "HDR Sync", "filter": "Ha", "exp_s": 60, "passed_qa": True}] * 3
            + [{"target": "HDR Sync", "filter": "Ha", "exp_s": 600, "passed_qa": True}] * 5
            + [{"target": "HDR Sync", "filter": "Ha", "exp_s": 600, "passed_qa": False}])
    (runs.runs_dir(cfg) / "2026-09-20_subs.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    runs.sync_goal_progress(cfg)
    ha = next(p for p in store.projects[proj.id].exposure_plans
              if p.filter_type.value == "Ha")
    assert (ha.hdr_short_acquired, ha.acquired) == (3, 5)
