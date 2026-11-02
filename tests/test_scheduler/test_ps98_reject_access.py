"""PS-98: rejected Piggy subs reachable from the desktop without syncing them.

The star sidecars and record summaries of a night's rejects are served read
only (and outlive the deleted reject FITS); one reject FITS can be fetched
while it is still on the scope PC; flexure-report runs through the remote
CLI on stored solves only; `photonscript reject-pull` saves them on the
desktop.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import httpx
import pytest

from photonscript.scheduler import remote_cli as rc
from photonscript.shared import star_table
from tests.test_scheduler.test_ps21_grading import NIGHT, _cfg, _rec, _seed


def _night(tmp_path):
    """Three Piggy subs (two rejected, one with its FITS deleted) and one
    RC16 reject."""
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    _seed(cfg, [
        _rec("p_rej", rig="piggyback", passed_qa=False, reason="ecc 0.95",
             ecc=0.95),
        _rec("p_gone", rig="piggyback", passed_qa=False, reason="ecc 0.92"),
        _rec("p_ok", rig="piggyback"),
        _rec("r_rej", rig="rc16", passed_qa=False),
    ])
    for name in ("p_rej", "p_gone"):
        t = star_table.build([10, 20], [30, 40], [2.0, 3.0], [0.9, 0.95],
                             theta=[0.1, 0.2], w=100, h=80, rig="piggyback",
                             grader="sep-binned", ecc_def="sqrt")
        star_table.write(cfg, NIGHT, f"LIGHT/{name}.fits", t, rig="piggyback")
    fits = Path(cfg.image_watch_dir) / NIGHT / "LIGHT"
    fits.mkdir(parents=True, exist_ok=True)
    (fits / "p_rej.fits").write_bytes(b"SIMPLE  =                    T")
    (fits / "p_ok.fits").write_bytes(b"SIMPLE  =                    T")
    return cfg


def test_night_stars_rejects_only_with_sidecars(tmp_path):
    from photonscript.scheduler.reject_access import night_stars
    cfg = _night(tmp_path)
    rep = night_stars(cfg, NIGHT)
    assert rep["rig"] == "piggyback" and rep["state"] == "rejected"
    by = {Path(s["file"]).stem: s for s in rep["subs"]}
    assert set(by) == {"p_rej", "p_gone"}           # no accepted, no RC16
    assert rep["count"] == 2 and rep["with_sidecar"] == 2
    assert by["p_rej"]["fits_on_scope"] is True
    assert by["p_gone"]["fits_on_scope"] is False   # deleted, sidecar kept
    assert by["p_gone"]["sidecar"]["n"] == 2
    assert by["p_rej"]["reason"] == "ecc 0.95" and by["p_rej"]["state"] == "rejected"
    assert len(by["p_rej"]["sidecar"]["ecc"]) == 2


def test_night_stars_all_and_missing_sidecar(tmp_path):
    from photonscript.scheduler.reject_access import night_stars
    cfg = _night(tmp_path)
    rep = night_stars(cfg, NIGHT, state="all")
    by = {Path(s["file"]).stem: s for s in rep["subs"]}
    assert by["p_ok"]["state"] == "accepted" and by["p_ok"]["sidecar"] is None
    assert night_stars(cfg, NIGHT, rig="rc16")["count"] == 1
    with pytest.raises(ValueError):
        night_stars(cfg, NIGHT, state="bogus")


def test_reject_fits_codes(tmp_path):
    from photonscript.scheduler.reject_access import reject_fits
    cfg = _night(tmp_path)
    code, p = reject_fits(cfg, NIGHT, "LIGHT/p_rej.fits")
    assert code == 200 and Path(p).name == "p_rej.fits"
    assert reject_fits(cfg, NIGHT, "LIGHT\\p_rej.fits")[0] == 200
    assert reject_fits(cfg, NIGHT, "LIGHT/p_ok.fits")[0] == 403
    assert reject_fits(cfg, NIGHT, "LIGHT/p_gone.fits")[0] == 404
    assert reject_fits(cfg, NIGHT, "LIGHT/nope.fits")[0] == 404
    assert reject_fits(cfg, NIGHT, "../armer_state.json")[0] == 400
    assert reject_fits(cfg, NIGHT, "C:/x.fits")[0] == 400


@pytest.fixture
def client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app
    cfg = _night(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    return TestClient(app.app, raise_server_exceptions=False)


def test_endpoints(client):
    r = client.get(f"/api/runs/{NIGHT}/stars/night")
    assert r.status_code == 200 and r.json()["count"] == 2
    assert client.get(f"/api/runs/{NIGHT}/stars/night",
                      params={"state": "x"}).status_code == 400
    r = client.get(f"/api/runs/{NIGHT}/reject-fits",
                   params={"file": "LIGHT/p_rej.fits"})
    assert r.status_code == 200 and r.content.startswith(b"SIMPLE")
    assert client.get(f"/api/runs/{NIGHT}/reject-fits",
                      params={"file": "LIGHT/p_ok.fits"}).status_code == 403
    assert client.get(f"/api/runs/{NIGHT}/reject-fits",
                      params={"file": "LIGHT/p_gone.fits"}).status_code == 404
    # the PS-80 single-sub route is still reached
    r = client.get(f"/api/runs/{NIGHT}/stars",
                   params={"file": "LIGHT/p_rej.fits", "compute": "false"})
    assert r.status_code == 200


def test_reject_pull_saves_sidecars_and_one_fits(client, tmp_path):
    from photonscript.cli import reject_pull

    def handler(req: httpx.Request) -> httpx.Response:
        r = client.get(req.url.path, params=dict(req.url.params))
        return httpx.Response(r.status_code, content=r.content,
                              headers={"content-type": r.headers.get(
                                  "content-type", "")})
    cl = httpx.Client(transport=httpx.MockTransport(handler))
    out = tmp_path / "desk" / "rejects"
    err = io.StringIO()
    rc_ = reject_pull("http://scope", NIGHT, out,
                      fits=["LIGHT/p_rej.fits", "LIGHT/p_gone.fits"],
                      client=cl, err=err)
    assert rc_ == 1                      # p_gone's FITS is deleted
    idx = json.loads((out / "index.json").read_text())
    assert {Path(s["file"]).stem for s in idx["subs"]} == {"p_rej", "p_gone"}
    assert all("sidecar" not in s for s in idx["subs"])
    assert (out / "p_gone.stars.json").exists()
    assert (out / "p_rej.fits").read_bytes().startswith(b"SIMPLE")
    assert "404" in err.getvalue()


def test_reject_pull_unreachable(tmp_path):
    from photonscript.cli import reject_pull

    def boom(req):
        raise httpx.ConnectError("down")
    cl = httpx.Client(transport=httpx.MockTransport(boom))
    assert reject_pull("http://scope", NIGHT, tmp_path / "o", client=cl,
                       err=io.StringIO()) == 2


def test_flexure_report_remote_needs_no_solve():
    allow = rc.load_allowlist()
    r = allow.commands["flexure-report"]
    assert r.cls == "read" and r.require == ("--no-solve",)
    assert rc.validate(["flexure-report", "--date", "2026-10-23", "--no-solve"],
                       allow).cls == "read"
    for args in (["flexure-report", "--date", "2026-10-23"],
                 ["flexure-report", "--no-solve", "--solve"],
                 ["flexure-report", "--no-solve", "--url", "http://x"]):
        with pytest.raises(rc.Refused):
            rc.validate(args, allow)
