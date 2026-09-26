"""Post-night autofocus-quality check (focus_reports)."""
import json

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.scheduler import focus_reports as fr


def _report(filter_="H", r2=0.95, hfr=2.3, points=7, ts="2026-09-25T23:21:33"):
    return {
        "Timestamp": ts,
        "Filter": filter_,
        "Temperature": 23.29,
        "CalculatedFocusPoint": {"Position": 5860.0, "Value": hfr},
        "RSquares": {"Quadratic": r2 - 0.02, "Hyperbolic": r2},
        "MeasurePoints": [{"FocuserPosition": 5900 + i, "Value": 3.0}
                          for i in range(points)],
    }


def test_best_r2_reads_dict_and_scalar():
    assert fr._best_r2({"RSquares": {"Quadratic": 0.8, "Hyperbolic": 0.93}}) == 0.93
    assert fr._best_r2({"RSquare": 0.71}) == 0.71
    assert fr._best_r2({"RSquares": {"Quadratic": "nan"}}) is None
    assert fr._best_r2({}) is None


def test_evaluate_passes_a_good_run():
    g = fr.evaluate(_report(r2=0.95, points=7), min_r2=0.7)
    assert g["ok"] is True and g["filter"] == "H" and g["r2"] == 0.95


def test_evaluate_flags_low_r2():
    g = fr.evaluate(_report(r2=0.30), min_r2=0.7)
    assert g["ok"] is False and "R^2" in g["reason"]


def test_evaluate_flags_too_few_points():
    g = fr.evaluate(_report(points=2), min_r2=0.7)
    assert g["ok"] is False and "point" in g["reason"]


def test_evaluate_unknown_r2_is_not_a_failure():
    rep = {"Filter": "L", "MeasurePoints": [{}, {}, {}, {}]}  # no R^2 at all
    assert fr.evaluate(rep)["ok"] is True


def test_load_reports_filters_by_night(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps(_report(ts="2026-09-25T23:00:00")))
    (tmp_path / "b.json").write_text(json.dumps(_report(ts="2026-09-26T05:00:00")))
    (tmp_path / "c.json").write_text(json.dumps(_report(ts="2026-09-20T23:00:00")))
    (tmp_path / "junk.json").write_text("{not valid")
    got = fr.load_reports(str(tmp_path), "2026-09-25")
    days = sorted(r["Timestamp"][:10] for r in got)
    assert days == ["2026-09-25", "2026-09-26"]  # evening + next morning, not the 20th


def test_check_and_alert_noop_without_dir():
    out = fr.check_and_alert(PhotonScriptConfig(), "2026-09-25")
    assert out["enabled"] is False and out["bad"] == []


def test_check_and_alert_fires_on_bad_run(tmp_path, monkeypatch):
    (tmp_path / "ok.json").write_text(json.dumps(_report(filter_="L", r2=0.96)))
    (tmp_path / "bad.json").write_text(json.dumps(_report(filter_="H", r2=0.25)))
    fired = []
    monkeypatch.setattr(fr, "_fire", lambda cfg, msg: fired.append(msg))
    cfg = PhotonScriptConfig(nina_autofocus_reports_dir=str(tmp_path))
    out = fr.check_and_alert(cfg, "2026-09-25")
    assert out["checked"] == 2 and len(out["bad"]) == 1
    assert out["bad"][0]["filter"] == "H"
    assert len(fired) == 1 and "Autofocus quality" in fired[0]


def test_check_and_alert_quiet_when_all_good(tmp_path, monkeypatch):
    (tmp_path / "ok.json").write_text(json.dumps(_report(r2=0.96)))
    fired = []
    monkeypatch.setattr(fr, "_fire", lambda cfg, msg: fired.append(msg))
    cfg = PhotonScriptConfig(nina_autofocus_reports_dir=str(tmp_path))
    out = fr.check_and_alert(cfg, "2026-09-25")
    assert out["bad"] == [] and fired == []
