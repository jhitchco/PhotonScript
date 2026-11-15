"""PS-260: the after-action email as a desktop pickup bundle (subject, text,
Gmail-safe HTML with cid:att0..attN, the images by index), the sent latch,
and the astro-dawn (early) build without the flats / shutdown lines."""
import asyncio
import io
import re
from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from photonscript.scheduler import after_action as aa
from photonscript.scheduler.routers import report as report_router
from photonscript.shared.config import PhotonScriptConfig

DATE = "2026-11-13"


def _png(color):
    buf = io.BytesIO()
    Image.new("RGB", (8, 4), color).save(buf, "PNG")
    return buf.getvalue()


TL_PNG, T0_PNG, T1_PNG = _png((1, 2, 3)), _png((200, 0, 0)), _png((0, 200, 0))


def _cfg(tmp_path, **kw):
    kw.setdefault("after_action_base_url", "https://teles.example")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


def _rep():
    return {"date": DATE, "title": f"AARO night {DATE}: 3 kept subs, 0.1 h",
            "lines": ["RC16: 2 kept / 1 rejected",
                      "Dawn flats 2026-11-13: RC16 0 shot, 0 filed (NINA stopped 05:52)",
                      "Dawn shutdown: parked, roof closed 06:10",
                      "Calibration owed: RC16: flats B 21 d"],
            "kept": 3, "rejected": 1, "hours": 0.1, "timeline_png": TL_PNG,
            "thumbs": [{"kind": "kept", "why": "best", "bytes": T0_PNG, "caption": "RC16 NGC 604 L 120 s"},
                       {"kind": "rejected", "why": "typical", "bytes": T1_PNG, "caption": "RC16 NGC 891 L 120 s"}],
            "links": [{"target": "NGC 604", "label": "Progression sheet",
                       "url": "https://claude.ai/artifact/YTZwa8BcKopRU6zyWQzSDc"}],
            "url": f"https://teles.example/report/{DATE}"}


def _client(tmp_path, monkeypatch, cfg=None):
    cfg = cfg or _cfg(tmp_path)
    aa._bundles.clear()
    calls = []

    def fake_build(c, d):
        calls.append(d)
        return _rep()
    monkeypatch.setattr(aa, "build", fake_build)
    monkeypatch.setattr(report_router, "_cfg", lambda: cfg)
    app = FastAPI()
    app.include_router(report_router.router)
    return TestClient(app), cfg, calls


def test_cid_order_matches_images_and_html_has_no_data_uris(tmp_path, monkeypatch):
    c, _cfg_, _ = _client(tmp_path, monkeypatch)
    b = c.get(f"/api/report/{DATE}/email-bundle").json()
    assert b["subject"] == _rep()["title"] and b["sent"] is None and b["early"] is False
    assert b["report_url"] == f"https://teles.example/report/{DATE}"
    assert "_bytes" not in b
    assert re.findall(r'src="cid:(att\d+)"', b["html"]) == ["att0", "att1", "att2"]
    assert [im["cid"] for im in b["images"]] == ["att0", "att1", "att2"]
    assert [im["bytes_len"] for im in b["images"]] == [len(TL_PNG), len(T0_PNG), len(T1_PNG)]
    assert b["images"][0]["name"] == f"{DATE}_timeline.png"     # timeline first
    assert b["images"][1]["url"] == f"https://teles.example/api/report/{DATE}/email-bundle/image/1"
    assert all(im["mime"] == "image/png" for im in b["images"])
    assert "data:" not in b["html"] and "<style" not in b["html"]
    assert "max-width:960px" in b["html"]
    # the text body says what the page says
    assert b["text"].startswith(_rep()["title"]) and "RC16: 2 kept / 1 rejected" in b["text"]
    assert "YTZwa8BcKopRU6zyWQzSDc" in b["text"] and "YTZwa8BcKopRU6zyWQzSDc" in b["html"]


def test_image_endpoint_returns_the_bytes_in_bundle_order(tmp_path, monkeypatch):
    c, _cfg_, calls = _client(tmp_path, monkeypatch)
    c.get(f"/api/report/{DATE}/email-bundle")
    for n, want in enumerate((TL_PNG, T0_PNG, T1_PNG)):
        r = c.get(f"/api/report/{DATE}/email-bundle/image/{n}")
        assert r.status_code == 200 and r.headers["content-type"] == "image/png"
        assert r.content == want
    assert c.get(f"/api/report/{DATE}/email-bundle/image/3").status_code == 404
    assert len(calls) == 1          # images come from the bundle just built
    # no bundle built yet for this key: built on demand
    r = c.get(f"/api/report/{DATE}/email-bundle/image/0?early=1")
    assert r.status_code == 200 and r.content == TL_PNG and len(calls) == 2


def test_early_mode_omits_flats_and_shutdown_lines(tmp_path, monkeypatch):
    c, _cfg_, _ = _client(tmp_path, monkeypatch)
    b = c.get(f"/api/report/{DATE}/email-bundle?early=1").json()
    assert b["early"] is True and "(at astro dawn)" in b["subject"]
    for body in (b["text"], b["html"]):
        assert "Dawn flats" not in body and "shutdown" not in body.lower()
        assert "RC16: 2 kept / 1 rejected" in body
        assert "Calibration owed" in body        # library state, not tonight's flats
    assert b["images"][0]["url"].endswith("/image/0?early=1")
    full = c.get(f"/api/report/{DATE}/email-bundle").json()
    assert "Dawn flats" in full["text"] and "Dawn shutdown" in full["html"]


def test_sent_latch_stops_a_second_smtp_send_but_not_the_push(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, after_action_mode="both")
    c, cfg, _ = _client(tmp_path, monkeypatch, cfg)
    r = c.post(f"/api/report/{DATE}/sent", json={"via": "desktop-gmail", "message_id": "abc123"})
    assert r.status_code == 200 and r.json()["sent"]["message_id"] == "abc123"
    sent = c.get(f"/api/report/{DATE}/email-bundle").json()["sent"]
    assert sent["via"] == "desktop-gmail" and sent["message_id"] == "abc123"

    pushes, mails = [], []

    async def notify(cfg, msg, **kw):
        pushes.append(msg)

    res = asyncio.run(aa.send(cfg, DATE, notify=notify,
                              smtp=lambda c_, r_: mails.append(r_) or "email sent"))
    assert mails == [] and len(pushes) == 1
    assert res["steps"][0] == "push sent" and res["steps"][1].startswith("email skipped")
    # the manual send ignores the latch
    asyncio.run(aa.send(cfg, DATE, notify=notify, force=True,
                        smtp=lambda c_, r_: mails.append(r_) or "email sent"))
    assert len(mails) == 1


def test_email_record_keeps_the_dawn_loop_due_and_its_mark_keeps_the_record(tmp_path):
    cfg = _cfg(tmp_path, after_action_after_shutdown_min=40)

    class A:
        shutdown = {"at": "2026-11-14T12:52:24Z"}
    at = datetime(2026, 11, 14, 12, 52, 24)
    aa.mark_email(cfg, DATE, via="desktop-gmail", message_id="m1", early=True)
    assert aa.due(cfg, A, at + timedelta(minutes=41)) == DATE    # push still due
    aa._mark(cfg, DATE, {"steps": ["push sent", "email skipped"]})
    assert aa.due(cfg, A, at + timedelta(minutes=50)) is None
    assert aa.email_sent(cfg, DATE)["message_id"] == "m1"


def test_smtp_success_records_the_email(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, after_action_mode="email")
    monkeypatch.setattr(aa, "build", lambda c, d: _rep())
    asyncio.run(aa.send(cfg, DATE, smtp=lambda c_, r_: "email sent to me@example.com"))
    assert aa.email_sent(cfg, DATE)["via"] == "smtp"
    mails = []
    asyncio.run(aa.send(cfg, DATE, smtp=lambda c_, r_: mails.append(1) or "email sent"))
    assert mails == []


def test_routes_are_defined():
    paths = {getattr(r, "path", "") for r in report_router.router.routes}
    assert {"/api/report/{date}/email-bundle", "/api/report/{date}/email-bundle/image/{n}",
            "/api/report/{date}/sent"} <= paths
