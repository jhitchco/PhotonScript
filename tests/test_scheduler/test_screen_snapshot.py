"""Scope PC desktop screenshot (scheduler/screen_snapshot.py, routers/screen.py,
cli screen-snapshot / remote-screen). Fakes only: no real screen capture."""
from __future__ import annotations

import io
import json

import httpx
import pytest
from PIL import Image

from photonscript.scheduler import remote_cli as rc
from photonscript.scheduler import screen_snapshot as ss
from photonscript.shared.config import PhotonScriptConfig

TOKEN = "s" * 32
H = {"X-PhotonScript-Token": TOKEN}
JPEG_MAGIC = b"\xff\xd8\xff"


def _cfg(tmp_path, token=TOKEN):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              remote_cli_token=token)


def _fake_grab(calls=None, size=(400, 200)):
    def grab(monitor):
        if calls is not None:
            calls.append(monitor)
        return Image.new("RGB", size, (20, 40, 60))
    return grab


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


# ------------------------------------------------------------ pure logic

def test_parse_args_defaults_and_bounds():
    assert ss.parse_args() == (0.5, "all", 70)
    assert ss.parse_args("1", "PRIMARY", "40") == (1.0, "primary", 40)
    for kw in ({"scale": "0"}, {"scale": "2"}, {"scale": "x"}, {"monitor": "3"},
               {"quality": "5"}, {"quality": "99"}, {"quality": "q"}):
        with pytest.raises(ss.SnapshotRefused) as e:
            ss.parse_args(**kw)
        assert e.value.status == 400


def test_session_zero_refused_before_any_capture(tmp_path):
    calls = []
    with pytest.raises(ss.SnapshotRefused) as e:
        ss.take(_cfg(tmp_path), sid_fn=lambda: 0, grab_fn=_fake_grab(calls),
                limit=ss.RateLimit(5, Clock()))
    assert e.value.status == 409 and "session 0" in e.value.detail
    assert calls == []


def test_take_returns_scaled_jpeg(tmp_path):
    calls = []
    shot = ss.take(_cfg(tmp_path), "0.5", "primary", "60", sid_fn=lambda: 1,
                   grab_fn=_fake_grab(calls), limit=ss.RateLimit(5, Clock()))
    assert calls == ["primary"]
    assert shot.jpeg.startswith(JPEG_MAGIC)
    assert (shot.width, shot.height, shot.session_id) == (200, 100, 1)
    assert Image.open(io.BytesIO(shot.jpeg)).size == (200, 100)


def test_rate_limit_one_per_interval_shared_by_stamp_file(tmp_path):
    cfg = _cfg(tmp_path)
    clock = Clock()
    a = ss.RateLimit(5, clock)
    a.acquire(cfg)
    clock.t += 2
    with pytest.raises(ss.SnapshotRefused) as e:
        a.acquire(cfg)
    assert e.value.status == 429 and e.value.retry_after == 3
    # another process (a new limiter, same data_dir) sees the same stamp
    with pytest.raises(ss.SnapshotRefused):
        ss.RateLimit(5, clock).acquire(cfg)
    clock.t += 3.1
    a.acquire(cfg)


def test_size_cap_steps_quality_then_scale():
    noisy = Image.effect_noise((600, 600), 100).convert("RGB")
    b, w, h, s, q = ss.encode(noisy, 1.0, 90, max_bytes=40_000)
    assert len(b) <= 40_000 and b.startswith(JPEG_MAGIC)
    assert q == ss.QUALITY_MIN or s < 1.0
    with pytest.raises(ss.SnapshotRefused) as e:
        ss.encode(noisy, 1.0, 90, max_bytes=10)
    assert e.value.status == 507


def test_save_and_saved_file_names(tmp_path):
    cfg = _cfg(tmp_path)
    shot = ss.take(cfg, sid_fn=lambda: 1, grab_fn=_fake_grab(),
                   limit=ss.RateLimit(5, Clock()))
    p1, p2 = ss.save(cfg, shot), ss.save(cfg, shot)
    assert p1 != p2 and p1.parent == ss.screen_dir(cfg)
    assert ss.saved_file(cfg, p1.name) == p1
    assert ss.saved_file(cfg, p2.name) == p2
    for bad in ("../x.jpg", "last_capture.json", "screen_1.jpg", p1.name + "/.."):
        assert ss.saved_file(cfg, bad) is None


# ------------------------------------------------------------ endpoints

@pytest.fixture
def api(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.routers import screen as router
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    state = {"sid": 1, "calls": []}
    clock = Clock()
    monkeypatch.setattr(router, "_sid", lambda: state["sid"])
    monkeypatch.setattr(router, "_grab", _fake_grab(state["calls"]))
    monkeypatch.setattr(ss, "LIMIT", ss.RateLimit(5, clock))
    c = TestClient(app_mod.app)
    c.cfg, c.state, c.clock = cfg, state, clock
    return c


def test_endpoint_needs_the_remote_cli_token(api):
    api.cfg.remote_cli_token = ""
    r = api.get("/api/screen/snapshot", headers=H)
    assert r.status_code == 403 and "disabled" in r.json()["detail"]
    api.cfg.remote_cli_token = TOKEN
    assert api.get("/api/screen/snapshot").status_code == 401
    r = api.get("/api/screen/snapshot", headers={"X-PhotonScript-Token": "w" * 32})
    assert r.status_code == 401
    assert api.get("/api/screen/files/screen_20261018T120000Z.jpg").status_code == 401
    assert api.state["calls"] == []
    ev = rc.read_audit(api.cfg)
    assert [e["event"] for e in ev] == ["screen_refused"] * 4
    assert TOKEN not in json.dumps(ev)


def test_endpoint_returns_jpeg_and_audits(api):
    r = api.get("/api/screen/snapshot", params={"scale": "0.25", "monitor": "all"},
                headers=H)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/jpeg"
    assert r.content.startswith(JPEG_MAGIC)
    assert (r.headers["x-screen-width"], r.headers["x-screen-height"]) == ("100", "50")
    assert r.headers["x-screen-session"] == "1"
    assert r.headers["cache-control"] == "no-store"
    assert api.state["calls"] == ["all"]
    a = rc.read_audit(api.cfg)[0]
    assert a["event"] == "screen" and a["bytes"] == len(r.content)


def test_endpoint_rate_limit(api):
    assert api.get("/api/screen/snapshot", headers=H).status_code == 200
    api.clock.t += 1
    r = api.get("/api/screen/snapshot", headers=H)
    assert r.status_code == 429 and r.headers["retry-after"] == "4"
    assert len(api.state["calls"]) == 1
    api.clock.t += 5
    assert api.get("/api/screen/snapshot", headers=H).status_code == 200


def test_endpoint_session_zero_refused(api):
    api.state["sid"] = 0
    r = api.get("/api/screen/snapshot", headers=H)
    assert r.status_code == 409 and "session 0" in r.json()["detail"]
    assert api.state["calls"] == []


def test_endpoint_bad_args_and_capture_failure(api, monkeypatch):
    from photonscript.scheduler.routers import screen as router
    assert api.get("/api/screen/snapshot", params={"scale": "5"},
                   headers=H).status_code == 400

    def boom(monitor):
        raise OSError("screen grab failed")
    monkeypatch.setattr(router, "_grab", boom)
    r = api.get("/api/screen/snapshot", headers=H)
    assert r.status_code == 503 and "locked" in r.json()["detail"]


def test_endpoint_serves_saved_file(api):
    shot = ss.take(api.cfg, sid_fn=lambda: 1, grab_fn=_fake_grab(),
                   limit=ss.RateLimit(5, Clock()))
    p = ss.save(api.cfg, shot)
    r = api.get(f"/api/screen/files/{p.name}", headers=H)
    assert r.status_code == 200 and r.content == shot.jpeg
    assert r.headers["content-type"] == "image/jpeg"
    assert api.get("/api/screen/files/screen_20000101T000000Z.jpg",
                   headers=H).status_code == 404
    assert api.get("/api/screen/files/last_capture.json", headers=H).status_code == 404


# ------------------------------------------------------------ CLI

def test_cli_screen_snapshot_writes_under_data_dir(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from photonscript import cli
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_a, **_k: cfg)
    monkeypatch.setattr(ss, "grab_pil", _fake_grab())
    monkeypatch.setattr(ss, "LIMIT", ss.RateLimit(5, Clock()))
    import photonscript.app_start as ast
    monkeypatch.setattr(ast, "session_id", lambda: 1)
    res = CliRunner().invoke(cli.app, ["screen-snapshot", "--json"])
    assert res.exit_code == 0, res.output
    d = json.loads(res.output)
    assert d["ok"] and d["name"].startswith("screen_")
    assert ss.saved_file(cfg, d["name"]).read_bytes().startswith(JPEG_MAGIC)
    # rate limited (same clock)
    res = CliRunner().invoke(cli.app, ["screen-snapshot", "--json"])
    assert res.exit_code == 2 and json.loads(res.output)["status"] == 429


def test_cli_screen_snapshot_session_zero(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from photonscript import cli
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_a, **_k: _cfg(tmp_path))
    import photonscript.app_start as ast
    monkeypatch.setattr(ast, "session_id", lambda: 0)
    res = CliRunner().invoke(cli.app, ["screen-snapshot"])
    assert res.exit_code == 2 and "session 0" in res.output


def _jpeg_server(status=200):
    seen = {}

    def handler(req: httpx.Request):
        seen["token"] = req.headers.get("X-PhotonScript-Token")
        seen["params"] = dict(req.url.params)
        seen["path"] = req.url.path
        if status != 200:
            return httpx.Response(status, json={"detail": "one screen snapshot per 5 s"})
        return httpx.Response(200, content=JPEG_MAGIC + b"rest",
                              headers={"content-type": "image/jpeg",
                                       "x-screen-width": "100", "x-screen-height": "50"})
    return httpx.Client(transport=httpx.MockTransport(handler)), seen


def test_remote_screen_fetch_writes_jpeg(tmp_path):
    from photonscript.cli import remote_screen_fetch
    cl, seen = _jpeg_server()
    out, err = tmp_path / "o" / "s.jpg", io.StringIO()
    assert remote_screen_fetch("http://scope:8100/", TOKEN, out, scale=0.25,
                               monitor="primary", client=cl, err=err) == 0
    assert out.read_bytes() == JPEG_MAGIC + b"rest"
    assert seen["token"] == TOKEN and seen["path"] == "/api/screen/snapshot"
    assert seen["params"] == {"scale": "0.25", "monitor": "primary", "quality": "70"}
    assert TOKEN not in err.getvalue()


def test_remote_screen_fetch_refused_and_unreachable(tmp_path):
    from photonscript.cli import remote_screen_fetch
    cl, _ = _jpeg_server(429)
    err = io.StringIO()
    out = tmp_path / "s.jpg"
    assert remote_screen_fetch("http://s", TOKEN, out, client=cl, err=err) == 2
    assert "429" in err.getvalue() and not out.exists()

    def boom(req):
        raise httpx.ConnectError("down")
    err = io.StringIO()
    assert remote_screen_fetch("http://s", TOKEN, out, err=err,
                               client=httpx.Client(transport=httpx.MockTransport(boom))) == 2
    assert "unreachable" in err.getvalue()


def test_cli_remote_screen_uses_the_env_token(monkeypatch, tmp_path):
    from typer.testing import CliRunner
    from photonscript import cli
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_a, **_k: _cfg(tmp_path))
    got = {}

    def fake(url, token, out_path, **kw):
        got.update(url=url, token=token, out=out_path, **kw)
        return 0
    monkeypatch.setattr(cli, "remote_screen_fetch", fake)
    res = CliRunner().invoke(cli.app, ["remote-screen", "--out", str(tmp_path / "x.jpg"),
                                       "--url", "http://s:8100", "--scale", "1"])
    assert res.exit_code == 0, res.output
    assert got["token"] == TOKEN and got["url"] == "http://s:8100" and got["scale"] == 1.0
    assert TOKEN not in res.output
    monkeypatch.delenv("PS_REMOTE_CLI_TOKEN", raising=False)
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_a, **_k: _cfg(tmp_path, ""))
    res = CliRunner().invoke(cli.app, ["remote-screen"])
    assert res.exit_code == 2 and "PS_REMOTE_CLI_TOKEN" in res.output
