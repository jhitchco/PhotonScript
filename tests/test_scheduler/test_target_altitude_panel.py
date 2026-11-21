"""2026-11-08: the Targets page shows where the target is in the sky.

Jeremy, on the NGC 604 page: "can we have a feature that shows where the
object is in the sky (the altitude)?" The dashboard already drew a noon-to-
noon altitude chart per scheduled target (inline altChartSVG); it moved to
static/js/alt_chart.js and the target page draws it under "In the sky
tonight" with a one-line summary (now, transit, above 30 deg), redrawn every
5 min.

Pinned: /api/target/altitude recomputes "now" on a cache hit (it used to
return the first call's now_frac all day), the cache follows the noon-to-
noon axis, and both pages load the shared chart.
"""
from pathlib import Path

import pytest

from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import TelescopeState

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"


def _read(*parts) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    monkeypatch.setattr(app, "_config", PhotonScriptConfig(
        _env_file=None, data_dir=tmp_path / "data",
        image_watch_dir=str(tmp_path / "fits"),
        nina_logs_dir=str(tmp_path / "logs")))
    monkeypatch.setattr(app, "_syncthing_pending_names", lambda: None)
    monkeypatch.setattr(app, "_rig_states", {})
    monkeypatch.setattr(app, "_telescope_state", TelescopeState())
    monkeypatch.setattr(app, "_alt_cache", {})
    return TestClient(app.app, raise_server_exceptions=False)


def test_altitude_now_is_fresh_on_a_cache_hit(client):
    from photonscript.scheduler import app
    q = "/api/target/altitude?name=NGC%20604&ra_hours=1.5758&dec_degrees=30.783"
    r1 = client.get(q)
    assert r1.status_code == 200, r1.text
    d1 = r1.json()
    assert len(d1["alts"]) == 97 and 0.0 <= d1["now_frac"] <= 1.0
    assert len(app._alt_cache) == 1
    (key, cached), = app._alt_cache.items()
    assert key[2].endswith(("T00", "T01", "T02", "T03", "T04", "T05", "T06",
                            "T07", "T08", "T09", "T10", "T11", "T12", "T13",
                            "T14", "T15", "T16", "T17", "T18", "T19", "T20",
                            "T21", "T22", "T23"))   # the axis start, not a date
    cached["now_frac"] = -5.0                       # a stale "now"
    d2 = client.get(q).json()
    assert len(app._alt_cache) == 1                 # served from the cache
    assert d2["alts"] == d1["alts"]
    assert 0.0 <= d2["now_frac"] <= 1.0
    assert abs(d2["now_frac"] - d1["now_frac"]) < 0.01
    assert app._alt_cache[key]["now_frac"] == -5.0  # the hit copies, never mutates


def test_shared_chart_script():
    js = _read("static", "js", "alt_chart.js")
    assert "function altChartSVG(d, opts)" in js
    assert "window.altChartSVG = altChartSVG;" in js
    assert "window.AltChart = {svg: altChartSVG, altAt: altAt, localAt: localAt," in js
    assert "opts.fluid" in js and "opts.nowDot" in js
    assert "function summary(d)" in js


def test_dashboard_uses_the_shared_chart():
    s = _read("templates", "dashboard.html")
    assert '<script src="/static/js/alt_chart.js?v={{ version }}"></script>' in s
    assert "function altChartSVG(d) {" not in s     # no inline copy left
    assert s.count("altChartSVG(alt)") == 1 and s.count("altChartSVG(d)") == 2


def test_target_page_draws_the_altitude_panel():
    s = _read("templates", "target.html")
    assert '<script src="/static/js/alt_chart.js?v={{ version }}"></script>' in s
    assert 'id="altPanel" hidden' in s and "In the sky tonight" in s
    assert "async function loadAlt()" in s
    assert "'/api/target/altitude?name=' + encodeURIComponent(D.target)" in s
    assert "altChartSVG(d, {fluid: true, nowDot: true})" in s
    assert "if (!D.unattributed) { loadRef(); pollLive(); loadAlt(); }" in s
    # redrawn every 5 min while the tab is visible
    assert "setInterval(() => { if (!document.hidden) loadAlt(); }, 300000)" in s
