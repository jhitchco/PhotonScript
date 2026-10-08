"""PS-166: the morning report card (dashboard card + dawn push lines).

Per rig from the data other passes already keep: lights kept / rejected
with the top reject drivers, hours per goal, guiding health (guard summary,
PS-155 no-corrections, PS-165 unguided-in-name subs), stalls (NINA watchdog
run events), the dawn filing line (PS-157) and the calibration line (PS-160).
"""
import json
from pathlib import Path

import pytest

from photonscript.scheduler import morning_report as mr
from photonscript.scheduler import runs
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import (CelestialTarget, ExposurePlan,
                                        FilterType, ImagingProject)
from photonscript.shared.night_events import events_path

NIGHT = "2026-10-06"


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              piggyback_enabled=True, **kw)


def _m31():
    p = ImagingProject(
        id="m31", active=True,
        target=CelestialTarget(name="Andromeda Galaxy", ra_hours=0.71,
                               dec_degrees=41.27),
        exposure_plans=[ExposurePlan(filter_type=FilterType("L"),
                                     exposure_seconds=300, count=40)],
        budget_hours=20.0)
    p.total_integration_hours = 6.5
    return p


def _sub(i, **kw):
    r = {"rig": "rc16", "file": f"s{i}.fits", "target": "Andromeda Galaxy",
         "filter": "L", "exp_s": 300.0, "passed_qa": True, "reviewed": True,
         "guide_state": "guiding", "guide_rms": 0.5 + i * 0.1,
         "time": f"2026-10-07T04:{i:02d}:00"}
    r.update(kw)
    return r


def _write(cfg, night, recs):
    p = runs.runs_dir(cfg) / f"{night}_subs.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")


def _setup(tmp_path):
    cfg = _cfg(tmp_path)
    _write(cfg, NIGHT, [
        _sub(0), _sub(1), _sub(2, reviewed=False),
        _sub(3, passed_qa=False, drivers=["ecc"]),
        _sub(4, passed_qa=False, drivers=["ecc", "hfr"]),
        _sub(5, passed_qa=False, drivers=["stars"]),
        _sub(6, passed_qa=False, drivers=["roof"]),
        _sub(7, target="Tracking Test", test=True),          # left out
        {"rig": "piggyback", "file": "p0.fits", "target": "Andromeda Galaxy",
         "filter": "OSC", "exp_s": 600.0, "passed_qa": True, "reviewed": True,
         "time": "2026-10-07T04:00:00Z"},
    ])
    p = events_path(cfg, NIGHT)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("".join(json.dumps(e) + "\n" for e in [
        {"t": "2026-10-07T03:00:00Z", "rig": "piggyback", "kind": "nina_watch",
         "value": "stuck"},
        {"t": "2026-10-07T03:30:00Z", "rig": "piggyback", "kind": "nina_watch",
         "value": "ok"},
        {"t": "2026-10-07T05:00:00Z", "rig": "rc16", "kind": "no_corrections",
         "value": "D7"}]), encoding="utf-8")
    store.append_jsonl(store.guard_path(cfg, NIGHT), {
        "event": "open", "id": "n1", "t_utc": "2026-10-07T05:00:00Z",
        "kind": "no_corrections", "codes": ["D7"]})
    return cfg


def test_card_per_rig(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration_owed
    monkeypatch.setattr(calibration_owed, "morning_note",
                        lambda cfg: "Calibration owed: RC16: flats L 50 d")
    cfg = _setup(tmp_path)
    card = mr.report_card(cfg, NIGHT, projects=[_m31()])
    rc, pb = card["rigs"]
    li = rc["lights"]
    assert (li["subs"], li["kept"], li["rejected"]) == (7, 3, 4)
    assert (li["approved"], li["review"]) == (2, 1)
    assert li["top_reasons"][0] == {"driver": "ecc", "n": 2}
    assert len(li["top_reasons"]) == mr.TOP_REASONS
    h = li["hours"][0]
    assert h["target"] == "Andromeda Galaxy" and h["hours"] == 0.25
    assert h["goal_total_h"] == 6.5 and h["goal_budget_h"] == 20.0
    g = rc["guiding"]
    assert g["no_corrections"] == 1 and g["status"] == "attention"
    assert g["rms_median"] == 0.8 and g["guided_subs"] == 7   # test sub out
    assert pb["lights"]["kept"] == 1 and "guiding" not in pb
    assert pb["stalls"] == [{"t": "2026-10-07T03:00:00Z", "state": "stuck"}]
    assert rc["stalls"] == []
    assert card["calibration"].startswith("Calibration owed")
    lines = card["lines"]
    assert lines[0].startswith("RC16: 3 kept / 4 rejected; 1 to review; "
                               "rejects: ecc 2")
    assert "Andromeda Galaxy 0.2 h (6.5/20 h)" in lines[0] or \
        "Andromeda Galaxy 0.3 h (6.5/20 h)" in lines[0]
    assert "1 no-corrections episode(s)" in lines[0]
    assert lines[1].startswith("Piggy-600: 1 kept / 0 rejected")
    assert "stalls: stuck 1" in lines[1]
    assert lines[-1].startswith("Calibration owed")


def test_library_line_and_latest_night(tmp_path, monkeypatch):
    from photonscript.scheduler import dawn_autofile
    cfg = _setup(tmp_path)
    _write(cfg, "2026-10-01", [_sub(0)])
    monkeypatch.setattr(dawn_autofile, "morning_line",
                        lambda c, d: f"Library: 3 subs filed for sync ({d})")
    assert mr.latest_night(cfg, today="2026-10-08") == NIGHT
    assert mr.latest_night(cfg, today="2026-10-02") == "2026-10-01"
    card = mr.report_card(cfg, projects=[], calibration=False)
    assert card["date"] == NIGHT and card["calibration"] is None
    assert "Library: 3 subs filed for sync (2026-10-06)" in card["lines"]


def test_no_subs_log(tmp_path):
    cfg = _cfg(tmp_path)
    card = mr.report_card(cfg, projects=[], calibration=False)
    assert card["date"] is None and card["rigs"] == []
    assert mr.card_lines(card) == ["Morning report: no subs log yet"]
    assert mr.push_text(cfg, None) is None
    # a night with no subs and no stalls says nothing in the push
    assert mr.push_text(cfg, NIGHT) is None


@pytest.mark.asyncio
async def test_night_complete_push_carries_the_card(tmp_path, monkeypatch):
    from photonscript.scheduler import armer as armer_mod
    from photonscript.scheduler import calibration_owed, integrations
    from photonscript.scheduler.armer import Armer
    import photonscript.scheduler.split_guard as sg
    sent = []

    async def _notify(cfg, msg, title="", priority=0, **kw):
        sent.append(msg)
    monkeypatch.setattr(armer_mod, "notify", _notify)
    monkeypatch.setattr(sg, "morning_split_note", lambda cfg, n: ("", False))
    monkeypatch.setattr(integrations, "morning_note", lambda cfg: "")
    monkeypatch.setattr(calibration_owed, "morning_note",
                        lambda cfg: "Calibration owed: x")
    cfg = _setup(tmp_path)
    a = Armer(cfg)
    a.plan = {"night_of": NIGHT}
    await a._notify_complete("Night complete.")
    msg = sent[-1]
    assert msg.startswith("Night complete.\nCalibration owed: x\nRC16: 3 kept")
    assert msg.count("Calibration owed") == 1     # not twice

    def boom(c, d):
        raise RuntimeError("bad")
    monkeypatch.setattr(mr, "report_card", lambda *a, **k: boom(a, k))
    await a._notify_complete("Night complete.")
    assert sent[-1] == "Night complete.\nCalibration owed: x"


def test_endpoint_and_dashboard_card(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    cfg = _setup(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    from photonscript.scheduler import calibration_owed
    monkeypatch.setattr(calibration_owed, "morning_note", lambda c: None)
    r = TestClient(app_mod.app).get(f"/api/morning/report?date={NIGHT}")
    assert r.status_code == 200
    d = r.json()
    assert d["date"] == NIGHT and d["calibration"] == "Calibration: nothing owed"
    html = (Path(app_mod.__file__).parent / "templates" / "dashboard.html"
            ).read_text(encoding="utf-8")
    assert 'id="morningCard"' in html and "/api/morning/report" in html
