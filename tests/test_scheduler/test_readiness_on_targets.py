"""Runs layout (2026-10-11): integration readiness moved off the Runs sidebar
onto the Targets pages. The Runs sidebar is just Nights; the Targets cards
show a compact per-rig chip and the target page the full per-rig breakdown,
desktop sync and the photonscript integrate command, both from the existing
/api/integration/readiness + /api/sync (static/js/readiness.js)."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from photonscript.scheduler import app

ROOT = Path(app.__file__).parent
TPL = ROOT / "templates"
JS = ROOT / "static" / "js" / "readiness.js"


def _aside(html):
    a = html.index("<aside")
    return html[a:html.index("</aside>", a)]


def _func(js, name):
    a = js.index("function " + name + "(")
    b = js.find("\nfunction ", a + 1)
    b2 = js.find("\n// ", a + 1)
    ends = [x for x in (b, b2) if x > 0]
    return js[a:min(ends) if ends else len(js)]


def test_runs_sidebar_is_nights_only():
    html = (TPL / "runs.html").read_text(encoding="utf-8")
    side = _aside(html)
    assert "readyBox" not in html and "loadReadiness" not in html
    assert "/api/integration/readiness" not in html
    assert "prepare-integration" not in html
    # Nights is the first heading; the night list comes before the link
    assert re.findall(r"<h2>([^<]*)</h2>", side) == ["Nights"]
    assert side.index("regradeAllBtn") < side.index('id="nightList"')
    link = side.index('href="/targets"')
    assert side.index('id="nightList"') < link
    assert "Integration readiness is on Targets" in side


def test_targets_list_renders_the_chip():
    html = (TPL / "targets.html").read_text(encoding="utf-8")
    assert "/static/js/readiness.js" in html
    assert 'data-rkey="' in html and "Readiness.key(t.name)" in html
    fn = _func(html, "loadReadiness")
    assert "Readiness.load()" in fn and "Readiness.chip(" in fn
    assert html.index("loadReadiness();") > html.index("fetch('/api/targets')")
    assert html.isascii()


def test_target_page_renders_the_full_section():
    html = (TPL / "target.html").read_text(encoding="utf-8")
    assert "/static/js/readiness.js" in html
    fn = _func(html, "loadReady")
    assert "Readiness.load()" in fn
    assert "Readiness.detail(r, all.synced, copyBtn)" in fn
    # a paused project is not in the report: the per-target endpoint
    assert "/api/targets/readiness?name=" in fn
    assert 'href="/calibration"' in fn
    assert html.isascii()


def test_module_uses_only_the_existing_endpoints():
    js = JS.read_text(encoding="utf-8")
    assert js.isascii()
    assert set(re.findall(r"fetch\('([^']+)'", js)) == {
        "/api/integration/readiness", "/api/sync"}


def test_pages_serve_the_module(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.shared.config import PhotonScriptConfig
    cfg = PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                             image_watch_dir=str(tmp_path / "fits"),
                             nina_logs_dir=str(tmp_path / "logs"))
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", type("S", (), {"projects": {}})())
    c = TestClient(app.app)
    for url in ("/targets", "/target?name=Heart%20Nebula"):
        r = c.get(url)
        assert r.status_code == 200 and "/static/js/readiness.js" in r.text, url
    r = c.get("/runs")
    assert r.status_code == 200 and "readyBox" not in r.text
    s = c.get("/static/js/readiness.js")
    assert s.status_code == 200 and "window.Readiness" in s.text


# --- the module in a JS engine against the real endpoint body -------------------

def _endpoint_body(tmp_path, monkeypatch):
    """GET /api/integration/readiness on the PS-81 fixture, plus one
    Piggy-600 entry shaped like target_readiness() for that rig."""
    import asyncio

    from tests.test_scheduler.test_ps81_readiness import _fixture
    cfg, _lib, projects, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_projects", {p.id: p for p in projects})
    monkeypatch.setattr(app, "_store", object())   # no project store load
    out = app.api_integration_readiness()
    if asyncio.iscoroutine(out):
        out = asyncio.run(out)
    out = json.loads(json.dumps(out))
    out["targets"].append({
        "target": "Heart Nebula", "rig": "piggyback",
        "filters": {"OSC": {"accepted_in_library": 348, "planned": 400,
                            "exposure_s": 120.0, "flats": 30}},
        "darks_by_exposure": {"120s": 40}, "bias": 60,
        "lights_in_library": 348, "ready": True,
        "command": '.\\deploy\\prepare-integration-osc.ps1 -Name "Heart Nebula"'})
    out["targets"].append({
        "target": "Empty Goal", "filters": {"L": {
            "accepted_in_library": 0, "planned": 10, "exposure_s": 180.0,
            "flats": 0}}, "darks_by_exposure": {"180s": 0}, "bias": 0,
        "lights_in_library": 0, "ready": False,
        "command": '.\\deploy\\prepare-integration.ps1 -Target "Empty Goal"'})
    return out


_DRIVER = """
globalThis.window = globalThis;
const DATA = %s;
globalThis.fetch = async (url) => ({json: async () => DATA[url]});
%s
(async () => {
  const r = await Readiness.load();
  const copy = (label, text) => '[' + label + ':' + text + ']';
  console.log(JSON.stringify({
    keys: Object.keys(r.byKey).sort(), synced: r.synced,
    heart: Readiness.chip(r.byKey[Readiness.key('Heart Nebula')], r.synced),
    empty: Readiness.chip(r.byKey[Readiness.key('Empty Goal')], r.synced),
    none: Readiness.chip(r.byKey['nope'], r.synced),
    unsynced: Readiness.chip(r.byKey[Readiness.key('Crescent Nebula')], false),
    cmds: r.byKey[Readiness.key('Heart Nebula')].map(Readiness.command),
    detail: r.byKey[Readiness.key('Heart Nebula')].map(
        t => Readiness.detail(t, r.synced, copy)),
    detail_unsynced: Readiness.detail(r.byKey['crescentnebula'][0], false, copy),
  }));
})();
"""


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_module_renders_endpoint_data_in_node(tmp_path, monkeypatch):
    body = _endpoint_body(tmp_path, monkeypatch)
    data = {"/api/integration/readiness": body,
            "/api/sync": {"library_synced": True}}
    f = tmp_path / "drive.js"
    f.write_text(_DRIVER % (json.dumps(data), JS.read_text(encoding="utf-8")),
                 encoding="utf-8")
    out = json.loads(subprocess.run(["node", str(f)], capture_output=True,
                                    text=True, encoding="utf-8",
                                    check=True).stdout)
    assert out["keys"] == ["crescentnebula", "emptygoal", "heartnebula"]
    assert out["synced"] is True
    # compact chips: per rig, accepted lights per filter, ready when synced
    assert "RC16 Ha 3 OIII 1 " in out["heart"]
    assert "Piggy-600 OSC 348 " in out["heart"] and out["heart"].count(">ready<") == 2
    assert "R 0" not in out["heart"]                  # only filters with lights
    assert "no accepted lights yet" in out["empty"]
    assert out["none"] == ""
    assert "sync in progress" in out["unsynced"]
    # the modern command per rig, the ps1 kept as the secondary hint
    assert out["cmds"] == [
        'photonscript integrate --target "Heart Nebula" --rig rc16',
        'photonscript integrate --target "Heart Nebula" --rig piggyback']
    rc, pb = out["detail"]
    assert "[copy command:photonscript integrate --target \"Heart Nebula\" --rig rc16]" in rc
    assert "&quot;Heart Nebula&quot; --rig rc16</code>" in rc
    assert "legacy staging script" in rc and "prepare-integration.ps1" in rc
    assert "Ha</b> 3/20 lights" in rc and "OIII</b> 1/20 lights" in rc
    assert "600s: 32, 180s: 0" in rc and "bias 50" in rc
    assert "not ready yet" in rc and "Library synced" in rc
    assert "lights ready on desktop" in rc
    assert pb.startswith("<div") and "Piggy-600" in pb and "ready to integrate" in pb
    assert "--rig piggyback" in pb and "prepare-integration-osc.ps1" in pb
    assert "sync in progress" in out["detail_unsynced"]
    assert "accepted, sync in progress" in out["detail_unsynced"]
