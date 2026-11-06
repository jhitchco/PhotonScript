"""Narrowband two-rig blend readiness (Heart Nebula, 2026-10-14).

Per two-rig NB goal: the accepted RC16 Ha / OIII / SII the NB blend would
use, the Piggy-600 master (posted integration ledger), ready or not by the
thresholds (blend_nb_min_ha_h / _oiii_h / _sii_h) and nights to each from
tonight's planned hours. Pinned: the 2026-10-13 Heart numbers, the
thresholds, the Piggy master from the ledger store, which goals are listed,
the morning card line, GET /api/blend-readiness and the CLI.
"""

import json

from typer.testing import CliRunner

from photonscript.scheduler import blend_readiness as br
from photonscript.shared import ledger as L
from photonscript.shared.config import PhotonScriptConfig
from tests.test_scheduler.test_heart_ha_first import _heart, _m31

PLANNED_OFF = {"Ha": 1.6, "OIII": 1.85, "SII": 1.9}   # 32 / 37 / 38 x 180 s


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def _ledger(cfg, project, rig, version, hours, ok=True, sbf=None):
    led = L.Ledger(campaign=project.target.name, rig=rig, run=f"{rig}_v{version}",
                   version=version, created_at="2026-10-11T14:19:49Z",
                   machine={"acquisition": {"hours": hours,
                                            "subs_by_filter": sbf or {}},
                            "integration": {"ok": ok}})
    L.save(cfg.data_dir / "ledgers" / project.id / f"v{version:03d}.json", led)


def _with_hours(p, ha=0.0, oiii=0.0, sii=0.0):
    for e in p.exposure_plans:
        h = {"Ha": ha, "OIII": oiii, "SII": sii}.get(e.filter_type.value)
        if h is not None and e.rig == "rc16":
            e.acquired_s = h * 3600
            e.acquired = int(h * 3600 // e.exposure_seconds)
    return p


# --- one goal -------------------------------------------------------------------

def test_heart_on_2026_10_13_is_not_ready(tmp_path):
    cfg = _cfg(tmp_path)
    r = br.target_readiness(cfg, _heart(), piggy={"version": 2, "hours": 3.0,
                                                  "run": "x", "date": "2026-10-11"},
                            planned=PLANNED_OFF)
    assert not r["ready"] and r["uses"] == [] and r["core"] is None
    ha, oiii, sii = (r["channels"][f] for f in ("Ha", "OIII", "SII"))
    assert (ha["subs"], ha["hours"], ha["need_h"]) == (0, 0.0, 2.0)
    assert (oiii["subs"], oiii["hours"]) == (6, 0.33)
    assert ha["nights"] == 2 and oiii["nights"] == 1 and sii["nights"] == 1
    assert r["line"].startswith("Heart Nebula NB blend: not ready: Ha 0.0/2 h")
    assert "Piggy master v2 3 h" in r["line"]


def test_ready_steps_ha_then_oiii_then_sho(tmp_path):
    cfg = _cfg(tmp_path)
    pig = {"version": 2, "hours": 3.0, "run": "x", "date": "d"}
    r = br.target_readiness(cfg, _with_hours(_heart(), ha=2.1), piggy=pig)
    assert r["ready"] and r["uses"] == ["Ha"] and r["core"] is None
    r = br.target_readiness(cfg, _with_hours(_heart(), ha=2.1, oiii=1.5), piggy=pig)
    assert r["uses"] == ["Ha", "OIII"] and r["core"] == "HOO"
    r = br.target_readiness(cfg, _with_hours(_heart(), ha=2.1, oiii=1.5, sii=1.6),
                            piggy=pig)
    assert r["core"] == "SHO" and "SHO core" in r["line"]


def test_no_piggy_master_is_not_ready(tmp_path):
    r = br.target_readiness(_cfg(tmp_path), _with_hours(_heart(), ha=3), piggy=None)
    assert not r["ready"] and "waits for a Piggy-600 master" in r["line"]


def test_thresholds_from_config(tmp_path):
    cfg = _cfg(tmp_path, blend_nb_min_ha_h=1.0, blend_nb_min_oiii_h=0.2)
    r = br.target_readiness(cfg, _with_hours(_heart(), ha=1.2, oiii=0.33),
                            piggy={"version": 1, "hours": 1, "run": "r", "date": "d"})
    assert r["ready"] and r["uses"] == ["Ha", "OIII"]


def test_nights_without_a_plan_is_none():
    assert br.nights_to(0.0, 2.0, None) is None
    assert br.nights_to(2.5, 2.0, None) == 0
    assert br.nights_to(0.5, 2.0, 0.75) == 2


# --- the store, the report and the morning line --------------------------------------

def test_piggy_master_from_the_ledger_store(tmp_path):
    cfg = _cfg(tmp_path)
    h = _heart()
    assert br.piggy_master(cfg, h) is None
    _ledger(cfg, h, "piggyback", 1, 1.0)
    _ledger(cfg, h, "piggyback", 2, 3.02, ok=False)        # failed: skipped
    _ledger(cfg, h, "rc16", 3, 0.5, sbf={"Ha": {"integrated": 10,
                                                "integrated_s": 1800}})
    pm = br.piggy_master(cfg, h)
    assert pm["version"] == 1 and pm["hours"] == 1.0
    rm = br.rc16_master(cfg, h)
    assert rm["filters"] == {"Ha": {"subs": 10, "hours": 0.5}}


def test_report_lists_two_rig_nb_goals_only(tmp_path):
    cfg = _cfg(tmp_path)
    rc16_only = _heart()
    rc16_only.id, rc16_only.target.name = "n604", "NGC 604"
    rc16_only.exposure_plans = [e for e in rc16_only.exposure_plans if e.rig == "rc16"]
    paused = _heart()
    paused.id, paused.active = "paused", False
    paused.target.name = "Soul Nebula"
    rep = br.report(cfg, [_heart(), _m31(), rc16_only, paused], planned={})
    assert [r["target"] for r in rep["targets"]] == ["Heart Nebula"]
    one = br.report(cfg, [_heart(), paused], target="Soul Nebula", planned={})
    assert [r["target"] for r in one["targets"]] == ["Soul Nebula"]
    assert br.report(cfg, [_m31()], planned={})["targets"] == []


def test_tonight_planned_hours(tmp_path):
    from datetime import datetime
    cfg = _cfg(tmp_path)
    got = br.tonight_planned(cfg, [_m31(), _heart()], datetime(2026, 10, 14, 4))
    h = got["Heart Nebula"]
    assert set(h) == {"Ha", "OIII", "SII"} and all(v > 0.5 for v in h.values())
    on = br.tonight_planned(_cfg(tmp_path, plan_nb_blend_ha_first=True),
                            [_m31(), _heart()], datetime(2026, 10, 14, 4))
    assert on["Heart Nebula"]["Ha"] >= 2.0 > h["Ha"]


def test_morning_card_line(tmp_path):
    from photonscript.scheduler import morning_report as mr
    cfg = _cfg(tmp_path)
    assert br.morning_line(cfg, [_m31()], planned={}) is None
    line = br.morning_line(cfg, [_heart()], planned={"Heart Nebula": PLANNED_OFF})
    assert line.startswith("Heart Nebula NB blend: not ready")
    card = {"date": "2026-10-13", "rigs": [], "blend": line}
    assert line in mr.card_lines(card)


def test_morning_report_card_carries_the_line(tmp_path, monkeypatch):
    from photonscript.scheduler import morning_report as mr
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(br, "tonight_planned", lambda *a, **k: {})
    card = mr.report_card(cfg, "2026-10-13", projects=[_heart()],
                          calibration=False, completeness=False)
    assert card["blend"].startswith("Heart Nebula NB blend: not ready")
    assert card["blend"] in card["lines"]


def test_format_report_text(tmp_path):
    rep = br.report(_cfg(tmp_path), [_heart()],
                    planned={"Heart Nebula": PLANNED_OFF})
    txt = br.format_report(rep)
    assert "Heart Nebula: not ready" in txt
    assert "RC16 Ha      0 subs   0.0 h / 2 h  ~2 nights at 1.6 h/night" in txt
    assert "none yet" in txt
    assert br.format_report({"targets": []}).startswith("No two-rig")


# --- endpoint and CLI ----------------------------------------------------------------

def test_api_blend_readiness(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.routers import targets as tr
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(tr, "_cfg", lambda: cfg)
    monkeypatch.setattr(tr, "_projects", lambda: [_heart(), _m31()])
    monkeypatch.setattr(br, "tonight_planned",
                        lambda *a, **k: {"Heart Nebula": PLANNED_OFF})
    r = TestClient(app_mod.app).get("/api/blend-readiness",
                                    params={"target": "Heart Nebula"})
    assert r.status_code == 200
    t = r.json()["targets"][0]
    assert t["target"] == "Heart Nebula" and t["ready"] is False
    assert t["channels"]["Ha"]["nights"] == 2


def test_cli_blend_readiness_json(tmp_path, monkeypatch):
    from photonscript import cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_: cfg)
    import photonscript.scheduler.calibration_plan as cp
    monkeypatch.setattr(cp, "load_projects", lambda c: [_heart()])
    monkeypatch.setattr(br, "tonight_planned",
                        lambda *a, **k: {"Heart Nebula": PLANNED_OFF})
    res = CliRunner().invoke(cli.app, ["blend-readiness", "--json"])
    assert res.exit_code == 0, res.output
    rep = json.loads(res.output)
    assert rep["targets"][0]["channels"]["OIII"]["subs"] == 6
    res = CliRunner().invoke(cli.app, ["blend-readiness", "--target", "Heart Nebula"])
    assert res.exit_code == 0 and "Heart Nebula: not ready" in res.output

