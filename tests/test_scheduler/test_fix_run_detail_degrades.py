"""Fix 2026-10-13: GET /api/runs/2026-10-06 (and /pointing) answered 500 on
the scope while /timeline and /review-summary for the same night were 200.
Reproduced: one non-finite number in the night's pointing sidecar
(<night>_pointing.jsonl) is enough. Python's json writes and reads NaN, the
API's JSON response refuses it, and merge_night() puts the record on every
sub (the subs log was already sanitized, the sidecar was not). Now the
sidecar reads NaN / Infinity back as None and skips damaged lines, the
response is sanitized, a damaged plan file is no plan, and a night_detail()
that still raises answers a degraded page (subs + the error), never 500.

The fixture has 10-06's shape: Piggy-600 lights only (Andromeda and "?"),
RC16 calibration frames, a parked dawn sub at RA 0 / Dec 0."""
import json

import pytest

from photonscript.shared import pointing
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-10-06"


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data",
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _night(tmp_path, plan_text=None):
    runs = tmp_path / "data" / "runs"
    runs.mkdir(parents=True)
    subs = [
        {"file": "LIGHT/2026-10-07_04-12-00__0.00_300.00s_0030.fits", "rig": "piggyback",
         "target": "Andromeda Galaxy", "filter": "OSC", "exp_s": 300.0, "type": "LIGHT",
         "passed_qa": True, "reviewed": True, "hfr": 2.69,
         "time": "2026-10-07T04:17:00Z"},
        {"file": "LIGHT/2026-10-07_06-11-35__0.00_300.00s_0086.fits", "rig": "piggyback",
         "target": "?", "filter": "OSC", "exp_s": 300.0, "type": "LIGHT",
         "passed_qa": False, "hfr": 13.84, "time": "2026-10-07T12:17:02Z"},
        {"file": "DARK/2026-10-07_12-30-00__-10.00_300.00s_0000.fits", "rig": "rc16",
         "target": "", "filter": "L", "exp_s": 300.0, "type": "DARK",
         "time": "2026-10-07T12:35:00Z"},
    ]
    (runs / f"{NIGHT}_subs.jsonl").write_text("".join(json.dumps(s) + "\n" for s in subs))
    lines = [
        json.dumps({"rig": "piggyback", "file": subs[0]["file"], "t": "2026-10-07T04:14:30Z",
                    "src": "mount-log", "mount_ra": 10.96, "mount_dec": 41.35,
                    "alt": float("nan"), "ha_h": float("inf"), "pier": "West"}),
        # the parked dawn sub
        json.dumps({"rig": "piggyback", "file": subs[1]["file"], "t": "2026-10-07T12:14:05Z",
                    "src": "mount-log", "mount_ra": 0.0, "mount_dec": 0.0, "alt": -0.6,
                    "pier": "East", "model_err_arcmin": float("nan")}),
        json.dumps({"rig": "rc16", "file": ["not", "a", "path"]}),
        json.dumps({"rig": {"x": 1}, "file": "LIGHT/x.fits"}),
        json.dumps([1, 2, 3]),
        '{"rig": "rc16", "file": "LIGHT/trunc',
    ]
    (runs / f"{NIGHT}_pointing.jsonl").write_text("\n".join(lines) + "\n")
    if plan_text is not None:
        (runs / f"{NIGHT}_plan.json").write_text(plan_text)


def test_load_reads_non_finite_as_none_and_skips_damaged_lines(tmp_path):
    cfg = _cfg(tmp_path)
    _night(tmp_path)
    pts = pointing.load(cfg, NIGHT)
    assert len(pts) == 2
    rec = pts[("piggyback", "LIGHT/2026-10-07_04-12-00__0.00_300.00s_0030.fits")]
    assert rec["alt"] is None and rec["ha_h"] is None and rec["mount_ra"] == 10.96
    s = pointing.summarize(pts.values())
    assert s["rigs"]["piggyback"]["subs"] == 2 and s["model"] is None
    json.dumps(s, allow_nan=False)


def test_append_record_writes_null_not_nan(tmp_path):
    cfg = _cfg(tmp_path)
    pointing.append_record(cfg, NIGHT, {"rig": "rc16", "file": "LIGHT/a.fits",
                                        "alt": float("nan"), "solved_ra": 1.0})
    text = pointing.sidecar_path(cfg, NIGHT).read_text()
    assert "NaN" not in text and json.loads(text)["alt"] is None


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    monkeypatch.setattr(app, "_syncthing_pending_names", lambda: None)
    return TestClient(app.app, raise_server_exceptions=False)


def test_run_detail_and_pointing_answer_200_on_the_damaged_night(tmp_path, client):
    _night(tmp_path, plan_text='{"targets": [{"name": "Andromeda Galaxy"}, 7]}')
    r = client.get(f"/api/runs/{NIGHT}?backfill=false")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert "error" not in d
    assert len(d["subs"]) == 3
    by_file = {s["file"]: s for s in d["subs"]}
    p = by_file["LIGHT/2026-10-07_04-12-00__0.00_300.00s_0030.fits"]["pointing"]
    assert p["mount_ra"] == 10.96 and p["alt"] is None
    assert d["pointing"]["rigs"]["piggyback"]["subs"] == 2
    r = client.get(f"/api/runs/{NIGHT}/pointing")
    assert r.status_code == 200, r.text[:300]
    assert len(r.json()["records"]) == 2


def test_a_damaged_plan_file_is_no_plan(tmp_path, client):
    _night(tmp_path, plan_text='{"targets": [ truncated')
    r = client.get(f"/api/runs/{NIGHT}?backfill=false")
    assert r.status_code == 200 and r.json()["plan"] is None


def test_night_detail_raising_degrades(tmp_path, client, monkeypatch):
    from photonscript.scheduler import runs
    _night(tmp_path)

    def _boom(*a, **k):
        raise KeyError("exposures")
    monkeypatch.setattr(runs, "night_detail", _boom)
    r = client.get(f"/api/runs/{NIGHT}?backfill=false")
    assert r.status_code == 200, r.text[:300]
    d = r.json()
    assert d["error"].startswith("night detail failed: KeyError")
    assert len(d["subs"]) == 3 and d["table"] == [] and d["plan"] is None
    # the keys the runs page reads without a guard
    assert d["report"]["safe_hours"] == 0.0 and d["phases"] == {}
    assert d["calibration"] == {"frames": {}, "advice": []} and "total" in d["score"]
