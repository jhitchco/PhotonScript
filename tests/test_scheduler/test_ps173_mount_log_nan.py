"""PS-173: /api/status/mount-log?date=2026-10-05 (and 10-06) returned HTTP 500.

Live bisect 2026-10-08: tail=27 answered 200, tail=28 answered 500, so one
line 28 from the end of the 10-05 night could not be serialized. Lines come
from JSON, so the only value strict JSON refuses is a non-finite float: the
agent wrote a NaN (ninaAPI reports NaN coordinates at times) and FastAPI's
encoder (allow_nan=False) raised. Writer now drops non-finite values, the
reader turns old NaNs into null."""
import json
import math
from datetime import datetime

import pytest

from photonscript.shared import mount_log
from photonscript.shared.config import PhotonScriptConfig


def _cfg(tmp_path):
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"))


def _write_night(cfg, night, rows):
    p = mount_log.log_path(cfg, night)
    p.parent.mkdir(parents=True, exist_ok=True)
    # json.dumps writes NaN / Infinity by default, the way append_jsonl did
    p.write_text("".join(json.dumps(r) + "\n" for r in rows) + "not json\n[1, 2]\n",
                 encoding="utf-8")
    return p


ROWS = [
    {"t": "2026-10-06T02:00:00Z", "rig": "rc16", "ra": 10.0, "dec": 20.0,
     "alt": 50.0, "az": 100.0, "pier": "East", "slewing": False,
     "tracking": True, "parked": False, "why": "start"},
    {"t": "2026-10-06T02:05:00Z", "rig": "rc16", "ra": 10.1, "dec": 20.0,
     "alt": float("nan"), "az": float("inf"), "pier": None, "slewing": False,
     "tracking": True, "parked": False, "why": "move"},
    {"t": "2026-10-06T02:06:00Z", "rig": "rc16", "ra": float("nan"),
     "dec": 20.0, "alt": 51.0, "az": 101.0, "pier": "East", "slewing": False,
     "tracking": True, "parked": False, "why": "heartbeat"},
]


def test_sample_from_nina_drops_non_finite_values():
    s = mount_log.sample_from_nina({"RightAscension": 1.0, "Declination": 2.0,
                                    "Altitude": "NaN", "Azimuth": float("nan"),
                                    "SideOfPier": "pierEast"})
    assert s["alt"] is None and s["az"] is None and s["ra"] == 15.0
    assert mount_log.sample_from_nina({"RightAscension": float("nan"),
                                       "Declination": 2.0}) is None
    json.dumps(s, allow_nan=False)


def test_load_cleans_old_nan_lines_and_skips_non_dicts(tmp_path):
    cfg = _cfg(tmp_path)
    _write_night(cfg, "2026-10-05", ROWS)
    lines = mount_log.load(cfg, "2026-10-05")
    assert len(lines) == 3
    assert lines[1]["alt"] is None and lines[1]["az"] is None
    assert lines[2]["ra"] is None
    # a line with no usable position is not a pointing
    assert mount_log.position_at(lines, datetime(2026, 10, 6, 2, 6, 30)) is None
    assert mount_log.position_at(lines, datetime(2026, 10, 6, 2, 5, 30))["ra"] == 10.1


@pytest.mark.parametrize("night", ["2026-10-05", "2026-10-06"])
def test_mount_log_endpoint_answers_200_with_nan_lines(tmp_path, monkeypatch, night):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    _write_night(cfg, night, ROWS)
    r = TestClient(app.app).get(f"/api/status/mount-log?date={night}")
    assert r.status_code == 200
    body = r.json()
    assert body["count"] == 3 and body["lines"][1]["alt"] is None
    assert all(not isinstance(v, float) or math.isfinite(v)
               for ln in body["lines"] for v in ln.values())
