"""PS-103: the Guiding tab (route, template, shared panel JS, live endpoint)."""
import re
from collections import Counter
from pathlib import Path

import pytest

from photonscript.shared.config import PhotonScriptConfig

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
JS = ROOT / "static" / "js" / "phd2_panels.js"


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path / "data")


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import phd2 as r
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))

    async def _no_probe(cfg):
        return {"ok": False, "note": "PHD2 not reachable", "age_s": 0.0}
    monkeypatch.setattr(r, "_probe_phd2", _no_probe)
    return TestClient(app.app)


def _ids(html: str) -> list[str]:
    return re.findall(r'\bid="([^"]+)"', html)


def _js_ids() -> set[str]:
    """Every element id phd2_panels.js looks up."""
    src = JS.read_text(encoding="utf-8")
    ids = set(re.findall(r"""(?:\$|setText|setHTML|\bon)\('([A-Za-z]\w*)'""", src))
    ids |= set(re.findall(r"querySelectorAll\('#(\w+)", src))
    return ids


def test_guiding_page_renders_every_section_and_js_id(client):
    r = client.get("/guiding")
    assert r.status_code == 200
    html = r.text
    ids = _ids(html)
    dup = [k for k, n in Counter(ids).items() if n > 1]
    assert not dup, dup
    order = ["liveSec", "auditSec", "selftestSec", "calSec", "guardSec",
             "tuneSec", "glogSec", "tpointSec"]
    assert [html.index(f'id="{s}"') for s in order] == sorted(
        html.index(f'id="{s}"') for s in order)               # top to bottom
    missing = _js_ids() - set(ids) - {"phd2Summary"}          # System page only
    assert not missing, missing
    assert "/static/js/phd2_panels.js" in html and "PHD2.initGuidingPage()" in html
    assert 'href="/guiding"' not in html                      # its own pill is hidden
    assert re.search(r'data-night="\d{4}-\d\d-\d\d"', html)


def test_every_page_has_the_guiding_pill(client):
    for path in ("/system", "/calibration", "/mosaic", "/runs"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert 'href="/guiding" class="nav-pill"' in r.text, path


def test_system_page_links_instead_of_duplicating_panels(client):
    html = client.get("/system").text
    ids = _ids(html)
    dup = [k for k, n in Counter(ids).items() if n > 1]
    assert not dup, dup                                        # calPanel/calStatus were doubled
    assert "phd2Summary" in ids and "PHD2.systemSummary('phd2Summary')" in html
    assert html.index('id="phd2Panel"') < html.index("<h2>Preflight Check</h2>")
    for gone in ("auditRows", "stRun", "hotpixCapture", "tuneRows", "calNow"):
        assert gone not in ids, gone
    assert "/api/phd2/audit/apply" not in html                 # acting happens on /guiding
    js = JS.read_text(encoding="utf-8")
    for anchor in ("auditSec", "selftestSec", "calSec", "guardSec", "tuneSec"):
        assert f"'{anchor}'" in js                             # summary links resolve
        assert f'id="{anchor}"' in client.get("/guiding").text


def test_panel_js_uses_existing_routes_and_dry_runs_first():
    js = JS.read_text(encoding="utf-8")
    from photonscript.scheduler.routers import phd2, triage
    paths = {rt.path for m in (phd2, triage) for rt in m.router.routes}
    for url in re.findall(r"'(/api/phd2/[a-z/-]+)", js):
        assert url.rstrip("/") in paths, url
    i_dry = js.index("dry_run: true")
    i_real = js.index("dry_run: false")
    assert i_dry < js.index("if (!dry.ok) return;") < js.index("confirm('Dry run for") < i_real
    assert "'/api/phd2/audit' + (refresh ? '?refresh=1' : '')" in js


def test_panel_js_brackets_balance_outside_strings():
    """No Node here: a crude check that braces, brackets and parens pair up
    once string literals, regex literals and comments are stripped."""
    src = JS.read_text(encoding="utf-8")
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"//[^\n]*", "", src)
    src = re.sub(r"'(?:\\.|[^'\\\n])*'", "''", src)
    src = re.sub(r'"(?:\\.|[^"\\\n])*"', '""', src)
    src = re.sub(r"/\[[^\]]*\]/g", "R", src)                  # the esc() regex
    pairs, stack = {")": "(", "]": "[", "}": "{"}, []
    for i, ch in enumerate(src):
        if ch in "([{":
            stack.append(ch)
        elif ch in pairs:
            assert stack and stack.pop() == pairs[ch], f"unbalanced {ch} at {i}"
    assert not stack


def test_live_endpoint_shape(client):
    d = client.get("/api/phd2/live").json()
    assert set(d) >= {"night", "armer_state", "phd2_ops", "guiding", "phd2",
                      "session_state", "target", "filter"}
    assert d["phd2"]["ok"] is False and d["phd2_ops"]["owner"] is None
    g = d["guiding"]
    for k in ("state", "rms_ra_arcsec", "rms_dec_arcsec", "rms_total_arcsec", "snr",
              "hfd_px", "guide_camera_exposure", "guide_binning", "pixel_scale_arcsec", "units"):
        assert k in g, k
    assert client.get("/api/phd2/live?probe=false").json()["phd2"] is None


async def test_probe_reads_phd2_once_per_ttl_and_never_raises(tmp_path, monkeypatch):
    from photonscript.scheduler.routers import phd2 as r
    from photonscript.telescope_agent import phd2_client
    calls = []

    class _Fake:
        def __init__(self, host, port, config=None):
            self.host, self.port = host, port

        async def connect(self):
            calls.append("connect")
            return True

        async def disconnect(self):
            calls.append("disconnect")

        async def get_app_state(self):
            return "Guiding"

        async def get_exposure(self):
            return 2000

        async def get_camera_binning(self):
            return 2

        async def get_pixel_scale(self):
            return 0.95

        async def get_lock_position(self):
            raise RuntimeError("no lock")                  # PHD2 error reply
    monkeypatch.setattr(phd2_client, "PHD2Client", _Fake)
    monkeypatch.setattr(r, "_probe", {"t": 0.0, "out": None, "client": None, "lock": None})
    cfg = _cfg(tmp_path)
    a = await r._probe_phd2(cfg)
    assert a["ok"] and a["app_state"] == "Guiding" and a["exposure_ms"] == 2000
    assert a["binning"] == 2 and a["pixel_scale"] == 0.95 and a["lock_position"] is None
    b = await r._probe_phd2(cfg)
    assert b["app_state"] == "Guiding" and calls == ["connect", "disconnect"]   # cached
    r._probe["t"] = 0.0

    async def _down(self):
        return False
    monkeypatch.setattr(_Fake, "connect", _down)
    c = await r._probe_phd2(cfg)
    assert c["ok"] is False and c["note"] == "PHD2 not reachable"


def test_new_sources_are_ascii_without_em_dashes():
    for p in (JS, ROOT / "templates" / "guiding.html", ROOT / "routers" / "phd2.py",
              Path(__file__)):
        assert all(b < 128 for b in p.read_bytes()), p
