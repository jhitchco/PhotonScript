"""Target links: a goal's external pages (progression contact sheets etc.),
stored on ImagingProject.links, set with PATCH /api/projects2/{id}
{"links": [...]}, shown on the target page and as a badge on /targets, and
edited from the desktop with `photonscript target-links`."""
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from photonscript.scheduler.project_store import ProjectStore, clean_links
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget, ImagingProject

M31_URL = "https://claude.ai/artifact/P5vqZKUQW2eQxyRhhaNRcq"
HEART_URL = "https://claude.ai/artifact/JBqqooj2QLSh45Ys6HJQbe"
CATS = "Cat's Eye Nebula"


def _cfg(tmp_path):
    return PhotonScriptConfig(_env_file=None, data_dir=tmp_path,
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"))


def _store(cfg):
    store = ProjectStore(cfg)
    p = next((x for x in store.projects.values() if x.target.name == CATS),
             None)
    if p is None:
        p = store.add_from_target(CelestialTarget(
            name=CATS, catalog_id="NGC 6543", ra_hours=17.98,
            dec_degrees=66.63, object_type="planetary nebula"),
            budget_hours=6)
    return store, p


# --- model + store ------------------------------------------------------------

def test_model_round_trip_and_legacy_default():
    p = ImagingProject(id="x", target=CelestialTarget(
        name="M31", ra_hours=0.71, dec_degrees=41.27),
        links=[{"label": "Progression sheet", "url": M31_URL}])
    d = p.model_dump(mode="json")
    assert d["links"] == [{"label": "Progression sheet", "url": M31_URL}]
    assert ImagingProject(**d).links[0].url == M31_URL
    d.pop("links")                           # projects.json before this field
    assert ImagingProject(**d).links == []


def test_store_update_persists_replaces_and_clears(tmp_path):
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    plans = [e.model_dump() for e in p.exposure_plans]
    store.update(p.id, links=[{"label": " Progression sheet ", "url": M31_URL},
                              {"label": "AstroBin", "url": "http://x.org/a"}])
    again = ProjectStore(cfg).projects[p.id]
    assert [(x.label, x.url) for x in again.links] == [
        ("Progression sheet", M31_URL), ("AstroBin", "http://x.org/a")]
    assert [e.model_dump() for e in again.exposure_plans] == plans  # no re-plan
    store.update(p.id, priority=60)                  # links left out: kept
    assert len(store.projects[p.id].links) == 2
    store.update(p.id, links=[])
    assert ProjectStore(cfg).projects[p.id].links == []


@pytest.mark.parametrize("bad", [
    "nope",
    [{"label": "", "url": M31_URL}],
    [{"label": "x" * 61, "url": M31_URL}],
    [{"label": "a", "url": "ftp://x.org/f"}],
    [{"label": "a", "url": "javascript:alert(1)"}],
    [{"label": "a", "url": "https://"}],
    [{"label": "a", "url": "https://x.org/a b"}],
    [{"label": "a", "url": M31_URL}, {"label": "A", "url": HEART_URL}],
    [{"label": f"l{i}", "url": M31_URL} for i in range(11)],
    ["just a string"],
])
def test_clean_links_rejects(bad):
    with pytest.raises(ValueError):
        clean_links(bad)


def test_clean_links_accepts_limits():
    ok = [{"label": "x" * 60, "url": f"https://x.org/{i}"} for i in range(1)]
    ok += [{"label": f"l{i}", "url": "HTTP://X.org"} for i in range(9)]
    assert len(clean_links(ok)) == 10
    assert clean_links(None) == []


# --- API + pages ---------------------------------------------------------------

@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from photonscript.scheduler import app
    cfg = _cfg(tmp_path)
    store, p = _store(cfg)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    monkeypatch.setattr(app, "_store", store)
    return store, p, TestClient(app.app)


def test_patch_sets_validates_and_clears(api):
    store, p, client = api
    body = {"links": [{"label": "Progression sheet", "url": M31_URL}]}
    r = client.patch(f"/api/projects2/{p.id}", json=body)
    assert r.status_code == 200
    assert r.json()["links"] == body["links"]          # in _project_json
    assert [x.url for x in store.projects[p.id].links] == [M31_URL]
    r = client.patch(f"/api/projects2/{p.id}",
                     json={"links": [{"label": "bad", "url": "file:///c:/x"}]})
    assert r.status_code == 400 and "http(s)" in r.json()["detail"]
    assert [x.url for x in store.projects[p.id].links] == [M31_URL]  # unchanged
    r = client.patch(f"/api/projects2/{p.id}", json={"links": []})
    assert r.status_code == 200 and r.json()["links"] == []


def test_target_detail_cards_and_page_carry_links(api):
    store, p, client = api
    store.update(p.id, links=[{"label": "Progression sheet", "url": HEART_URL}])
    d = client.get("/api/targets/detail", params={"name": CATS}).json()
    assert d["links"] == [{"label": "Progression sheet", "url": HEART_URL}]
    cards = client.get("/api/targets").json()["targets"]
    card = next(c for c in cards if c["name"] == CATS)
    assert card["links"][0]["url"] == HEART_URL
    page = client.get("/target", params={"name": CATS}).text
    assert 'id="tLinks"' in page and "function renderLinks" in page
    assert 'target="_blank" rel="noopener"' in page
    assert "/api/projects2/" in page and "links: links" in page
    assert "t.links" in client.get("/targets").text


# --- CLI against a fake server ----------------------------------------------------

class _Fake:
    def __init__(self):
        self.links = [{"label": "Old", "url": "https://x.org/old"}]
        self.patches = []


def _serve(fake):
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def do_GET(self):
            if self.path.startswith("/api/targets/detail?") and "M31" in self.path:
                self._send(200, {"target": "M31", "project_id": "pid-1",
                                 "links": fake.links})
            else:
                self._send(404, {"detail": "no project"})

        def do_PATCH(self):
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n))
            fake.patches.append((self.path, body))
            try:
                fake.links = clean_links(body["links"])
            except ValueError as e:
                return self._send(400, {"detail": str(e)})
            self._send(200, {"id": "pid-1", "links": fake.links})

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def fake_server():
    fake = _Fake()
    srv = _serve(fake)
    yield fake, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def _run(*args):
    from typer.testing import CliRunner

    from photonscript.cli import app
    return CliRunner().invoke(app, ["target-links", *args])


def test_cli_lists_adds_and_removes(fake_server):
    fake, url = fake_server
    r = _run("--target", "M31", "--url", url)
    assert r.exit_code == 0 and "Old: https://x.org/old" in r.output
    assert fake.patches == []                                  # list = GET only
    r = _run("--target", "M31", "--url", url, "--add", "Progression sheet",
             M31_URL)
    assert r.exit_code == 0, r.output
    assert fake.patches[-1] == ("/api/projects2/pid-1", {"links": [
        {"label": "Old", "url": "https://x.org/old"},
        {"label": "Progression sheet", "url": M31_URL}]})
    r = _run("--target", "M31", "--url", url, "--remove", "old", "--json")
    assert r.exit_code == 0
    assert json.loads(r.output)["links"] == [
        {"label": "Progression sheet", "url": M31_URL}]


def test_cli_errors(fake_server):
    fake, url = fake_server
    assert _run("--target", "Nope", "--url", url).exit_code == 1
    assert _run("--target", "M31", "--url", url, "--remove", "zzz").exit_code == 1
    r = _run("--target", "M31", "--url", url, "--add", "x", "ftp://bad")
    assert r.exit_code == 1 and "400" in r.output
    assert fake.links == [{"label": "Old", "url": "https://x.org/old"}]


def test_not_in_remote_allowlist():
    from photonscript.scheduler.remote_cli import ALLOWLIST_PATH
    assert "target-links" not in ALLOWLIST_PATH.read_text(encoding="utf-8")
