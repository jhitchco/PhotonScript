"""2026-10-24 Imaging Runs fixes, server side.

* JsonGzipMiddleware: GET /api/runs/2026-10-23 (673 subs) was 2.2 MB of
  JSON at ~200 KB/s over the tailnet (~10 s of "..."); gzipped 136 KB.
  Thumbnails and other binaries are never compressed.
* pointing.clear_centered: night 2026-10-23 flagged all 672 subs "off target
  (header, unconfirmed)" 64'-68' from NGC 604; NINA's Center log shows the
  first slew 64'-68' off and a converged re-slew, so the header offset is the
  mount model's own, not a miss.
"""
import gzip
import json

from fastapi import FastAPI
from fastapi.responses import JSONResponse, Response
from fastapi.testclient import TestClient

from photonscript.scheduler import nina_center_log as ncl
from photonscript.scheduler.compression import JsonGzipMiddleware, compressible
from photonscript.shared import pointing
from photonscript.shared.config import PhotonScriptConfig

BIG = {"subs": [{"file": f"LIGHT/x_{i}.fits", "hfr": 2.5, "note": "y" * 40}
                for i in range(2000)]}


def _app():
    a = FastAPI()
    a.add_middleware(JsonGzipMiddleware)

    @a.get("/big")
    def big():
        return JSONResponse(BIG)

    @a.get("/small")
    def small():
        return {"ok": True}

    @a.get("/png")
    def png():
        return Response(b"\x89PNG" + b"\0" * 5000, media_type="image/png")

    @a.get("/html")
    def html():
        return Response("<p>" + "x" * 5000 + "</p>", media_type="text/html")
    return a


def test_gzip_json_and_html_only():
    c = TestClient(_app())
    r = c.get("/big", headers={"Accept-Encoding": "gzip"})
    assert r.headers["content-encoding"] == "gzip"
    assert "Accept-Encoding" in r.headers["vary"]
    assert r.json() == BIG                          # the client inflates it
    raw = c.get("/big", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in raw.headers and raw.json() == BIG
    assert int(r.headers["content-length"]) < len(raw.content) / 5
    s = c.get("/small", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in s.headers      # under MIN_SIZE
    p = c.get("/png", headers={"Accept-Encoding": "gzip"})
    assert "content-encoding" not in p.headers and p.content.startswith(b"\x89PNG")
    h = c.get("/html", headers={"Accept-Encoding": "gzip"})
    assert h.headers["content-encoding"] == "gzip"
    assert gzip.compress(b"x")  # stdlib sanity
    assert not compressible("image/jpeg") and compressible("application/json; charset=utf-8")


def test_app_has_the_middleware():
    from photonscript.scheduler import app
    assert any(m.cls is JsonGzipMiddleware for m in app.app.user_middleware)


# ------------------------------------------------------------------ pointing

NGC604 = (23.637, 30.783)


def _rec(off, t="2026-10-24T03:21:34Z", src="header", flag="flag"):
    return {"rig": "rc16", "file": "LIGHT/a.fits", "t": t, "src": src,
            "mount_src": src, "mount_ra": 22.6828, "mount_dec": 30.08401,
            "pier": "West", "target": "NGC 604", "target_ra": NGC604[0],
            "target_dec": NGC604[1], "off_target_arcmin": off,
            "off_target_dir": "SW", "flag": flag,
            "note": "SW from NGC 604 (mount RA 01h31m Dec +30.1, pier W)"}


def _run(first, t="2026-10-24T03:18:00Z", converged=True, ra=NGC604[0]):
    return {"t_utc": t, "target_ra": ra, "target_dec": NGC604[1],
            "first_sep_arcmin": first, "final_sep_arcmin": 0.4,
            "threshold_arcmin": 1.0, "converged": converged, "attempts": 2,
            "side": "W"}


def test_centered_offset_clears_a_matching_header_flag():
    r = pointing.clear_centered(None, _rec(64.77), [_run(68.1)])
    assert r["flag"] == "" and r["centered_by"] == "2026-10-24T03:18:00Z"
    assert "mount model offset" in r["note"] and "68'" in r["note"]


def test_centered_offset_keeps_real_flags():
    # offset unlike the first-slew error: a real miss stays flagged
    assert pointing.clear_centered(None, _rec(30.0), [_run(68.1)])["flag"] == "flag"
    # a run that never converged, a later run, another target: no clearing
    for run in (_run(65.0, converged=False), _run(65.0, t="2026-10-24T04:00:00Z"),
                _run(65.0, ra=NGC604[0] + 5)):
        assert pointing.clear_centered(None, _rec(64.77), [run])["flag"] == "flag"
    # a plate solve or a gross miss is never touched
    solved = dict(_rec(64.77), solved_ra=22.7, solved_dec=30.1)
    assert pointing.clear_centered(None, solved, [_run(65.0)])["flag"] == "flag"
    gross = _rec(400.0, flag="off-target")
    assert pointing.clear_centered(None, gross, [_run(400.0)])["flag"] == "off-target"


def test_latest_center_run_wins_after_a_flip():
    runs_ = [_run(68.1, t="2026-10-24T03:18:00Z"),
             _run(34.0, t="2026-10-24T06:40:00Z")]
    east = _rec(36.0, t="2026-10-24T07:00:00Z")
    assert pointing.clear_centered(None, east, runs_)["flag"] == ""
    early = _rec(36.0, t="2026-10-24T05:00:00Z")   # before the flip: 68' run
    assert pointing.clear_centered(None, early, runs_)["flag"] == "flag"


def test_load_clears_stored_flags_from_the_center_file(tmp_path):
    cfg = PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                             nina_logs_dir=str(tmp_path / "nologs"))
    night = "2026-10-23"
    p = pointing.sidecar_path(cfg, night)
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps(_rec(64.77)) + "\n", encoding="utf-8")
    pointing._CENTER.clear()
    assert pointing.load(cfg, night)[("rc16", "LIGHT/a.fits")]["flag"] == "flag"
    ncl.pointing_dir(cfg).mkdir(parents=True)
    ncl.night_file(cfg, night).write_text(json.dumps(_run(66.3)) + "\n",
                                          encoding="utf-8")
    pointing._CENTER.clear()
    rec = pointing.load(cfg, night)[("rc16", "LIGHT/a.fits")]
    assert rec["flag"] == "" and rec["center_first_arcmin"] == 66.3
    assert pointing.summarize([rec])["flagged"] == 0
    assert pointing.compact(rec)["centered_by"] == "2026-10-24T03:18:00Z"
    pointing._CENTER.clear()
