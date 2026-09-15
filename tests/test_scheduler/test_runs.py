"""Tests for the Imaging Runs analyzer."""

import json

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
from photonscript.scheduler.runs import (append_sub_record, night_detail,
                                         night_score, list_runs,
                                         save_plan_snapshot)


def _config(tmp_path):
    return PhotonScriptConfig(data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def test_night_score_weighting():
    s = night_score(6.0, 6.0, 6.0, 100)
    assert s["total"] == 100
    s = night_score(6.0, 0, 0, 0)
    assert s["total"] == 0
    s = night_score(6.0, 3.0, 3.0, 0)   # half the night accepted, no plan credit
    assert s["total"] == 50  # 50%*0.4 + 100%*0.3 + 0


def test_plan_vs_actual_assembly(tmp_path):
    config = _config(tmp_path)
    target = NinaSequenceTarget(
        name="Crescent Nebula", ra_hours=20.2, dec_degrees=38.35,
        exposures=[ExposurePlan(filter_type=FilterType.HA,
                                exposure_seconds=300, count=23)])
    save_plan_snapshot(config, "2026-07-04",
                       {"dusk_utc": "x", "dawn_utc": "y", "dark_hours": 6.5},
                       [target])
    for i in range(3):
        append_sub_record(config, "2026-07-04", {
            "file": f"LIGHT/sub{i}.fits", "time": f"2026-07-04T05:0{i}:00Z",
            "target": "Crescent Nebula", "filter": "Ha", "exp_s": 300,
            "hfr": 6.9, "ecc": 0.31, "stars": 97, "background": 51.0,
            "passed_qa": i != 2, "reason": "" if i != 2 else "ecc 0.71",
        })
    d = night_detail(config, "2026-07-04", backfill=False)
    row = d["table"][0]
    assert row["planned"] == 23
    assert row["attempted"] == 3
    assert row["accepted"] == 2
    assert row["median_hfr"] == 6.9
    assert d["score"]["total"] >= 0
    runs = list_runs(config)
    assert runs[0]["date"] == "2026-07-04"
    assert runs[0]["has_plan"] is True
    assert runs[0]["subs_logged"] == 3
