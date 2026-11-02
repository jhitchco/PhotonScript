"""PS-213: the focus crib sheet (GET /api/focus/crib, page /focus).

Same fixture as test_focus_table (RC16 slope -8.25, L 5900, AFs between
about 15 and 24 C): each cell equals the focus table at that temperature,
rows outside the measured AF temperatures are marked extrapolated, an
untrusted model gives no grid, and the Piggy-600 row appears only from a
trusted Piggy model.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import focus_crib as fc
from photonscript.scheduler import focus_model as fm
from photonscript.scheduler import focus_table as ft
from tests.test_scheduler.test_focus_table import _cfg, _nights, _store


def test_trusted_grid_matches_the_table(tmp_path):
    cfg = _cfg(tmp_path)
    _store(cfg, _nights())
    crib = fc.build_crib(cfg)
    assert crib["trusted"] is True and crib["grid"] is not None
    assert [r["temp"] for r in crib["grid"]] == [0, 5, 10, 15, 20, 25]
    for r in crib["grid"]:
        tab = ft.build_table(cfg, temp=float(r["temp"]))
        for f in crib["filters"]:
            assert r["positions"][f] == tab["filters"][f]["position"], (r, f)
    lo, hi = crib["temp_range"]
    for r in crib["grid"]:
        assert r["extrapolated"] is (not (lo <= r["temp"] <= hi))
    # 0 / 5 / 10 C are colder than any AF in the fixture
    assert crib["grid"][0]["extrapolated"] is True
    assert crib["grid"][4]["extrapolated"] is False     # 20 C
    col = crib["columns"]["L"]
    assert col["offset"] == 0 and col["offset_source"] == "reference"
    assert col["last_af"] == "2026-10-10"     # the last night runs past midnight
    assert crib["columns"]["Ha"]["n_afs"] > 0
    assert crib["slope_steps_per_c"] == pytest.approx(-8.25, abs=1.5)


def test_untrusted_model_gives_no_grid(tmp_path):
    cfg = _cfg(tmp_path)
    _store(cfg, _nights()[:5])          # a handful of AFs: not trusted
    crib = fc.build_crib(cfg)
    assert crib["trusted"] is False
    assert crib["grid"] is None and crib["columns"] == {}
    assert crib["trust_reasons"]


def test_no_points_never_raises(tmp_path):
    cfg = _cfg(tmp_path)
    crib = fc.build_crib(cfg)
    assert crib["trusted"] is False and crib["grid"] is None


def _piggy_pts(n=12, slope=-4.0, noise=False):
    start = datetime(2026, 10, 5, 20, 0)
    out = []
    for i in range(n):
        t = 22.0 - i * 0.6
        out.append({"filter": fm.OSC_FILTER, "rig": "piggyback",
                    "temp": round(t, 2), "step": 40,
                    "position": round(3000 + slope * t
                                      + ((-1) ** i * 60 if noise else 0)),
                    "time": (start + timedelta(minutes=30 * i))
                    .strftime("%Y-%m-%dT%H:%M:%S") + ".0-06:00"})
    return out


def test_piggy_row_only_when_trusted(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    _store(cfg, _nights())
    (Path(cfg.data_dir) / fm.PB_POINTS_FILE).write_text(
        json.dumps(_piggy_pts()))
    pb = fc.build_crib(cfg)["piggyback"]
    assert pb["trusted"] is True and pb["n_afs"] == 12
    assert pb["positions"]["20"]["position"] == 3000 - 80
    assert pb["positions"]["0"]["extrapolated"] is True
    # too few AFs: listed, untrusted, no positions
    (Path(cfg.data_dir) / fm.PB_POINTS_FILE).write_text(
        json.dumps(_piggy_pts(n=4)))
    pb = fc.build_crib(cfg)["piggyback"]
    assert pb["trusted"] is False and pb["positions"] is None
    assert pb["reasons"]


def test_no_piggy_key_when_piggyback_off(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)
    _store(cfg, _nights())
    assert "piggyback" not in fc.build_crib(cfg)


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    _store(cfg, _nights())
    monkeypatch.setattr(app, "_config", cfg)
    return TestClient(app.app, raise_server_exceptions=False)


def test_endpoint_and_page(client):
    r = client.get("/api/focus/crib")
    assert r.status_code == 200
    body = r.json()
    assert body["trusted"] is True and len(body["grid"]) == 6
    # a cell equals GET /api/focus/table?temp= (the ticket's offline check)
    t = client.get("/api/focus/table", params={"temp": 20}).json()
    assert body["grid"][4]["positions"]["Ha"] == t["filters"]["Ha"]["position"]
    page = client.get("/focus")
    assert page.status_code == 200
    assert "/api/focus/crib" in page.text and "@media print" in page.text
    assert 'href="/focus"' not in page.text     # its own nav pill is hidden
    other = client.get("/disk")
    assert 'href="/focus"' in other.text
