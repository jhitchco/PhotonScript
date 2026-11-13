"""PS-254: after-action report (card text + timeline PNG + a few thumbnails
+ contact-sheet links), as a page, a Pushover with the timeline, and an
optional SMTP email with the images inline."""
import asyncio
import io
from datetime import datetime, timedelta

from PIL import Image

from photonscript.scheduler import after_action as aa
from photonscript.shared.config import PhotonScriptConfig

T0 = datetime(2026, 11, 12, 1, 0)


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


def _iso(dt):
    return dt.isoformat() + "Z"


TL = {"ok": True, "t0": _iso(T0), "t1": _iso(T0 + timedelta(hours=10)),
      "rows": [{"id": "mount", "label": "Mount", "segments": [
          {"start": _iso(T0), "end": _iso(T0 + timedelta(hours=2)), "state": "tracking"},
          {"start": _iso(T0 + timedelta(hours=2)), "end": _iso(T0 + timedelta(hours=3)),
           "state": "parked"}]},
               {"id": "rc16", "label": "RC16", "segments": [
          {"start": _iso(T0), "end": _iso(T0 + timedelta(minutes=5)), "state": "autofocus"}]}]}


def _sub(rig, target, kept, hfr, i, reason=""):
    a = T0 + timedelta(minutes=5 * i)
    return {"rig": rig, "target": target, "filter": "L" if rig == "rc16" else "OSC",
            "file": f"LIGHT/{rig}_{i}.fits", "exp_s": 120.0, "hfr": hfr,
            "passed_qa": kept, "reason": reason,
            "start_utc": _iso(a), "end_utc": _iso(a + timedelta(minutes=2))}


SUBS = [_sub("rc16", "NGC 604", True, 7.9, 1), _sub("rc16", "NGC 604", True, 7.1, 2),
        _sub("rc16", "NGC 604", False, 11.0, 3, "FWHM 4.4\" > 4\"; HFR 11 px"),
        _sub("rc16", "NGC 604", False, 12.0, 4, "FWHM 4.9\" > 4\""),
        _sub("rc16", "NGC 891", False, 9.0, 5, "off target 26 deg S"),
        _sub("piggyback", "M33 Core P1", True, 2.1, 6), _sub("piggyback", "M33 Core P1", True, 1.8, 7)]


def test_pick_thumbs_best_kept_per_target_and_one_typical_reject_per_rig():
    picks = aa.pick_thumbs(SUBS)
    kept = [(p["sub"]["rig"], p["sub"]["file"]) for p in picks if p["kind"] == "kept"]
    assert kept == [("piggyback", "LIGHT/piggyback_7.fits"), ("rc16", "LIGHT/rc16_2.fits")]
    rej = [p for p in picks if p["kind"] == "rejected"]
    assert len(rej) == 1 and rej[0]["sub"]["rig"] == "rc16"
    assert "FWHM" in rej[0]["why"] and "(3 total)" in rej[0]["why"]


def test_timeline_png_draws_rows_and_subs(tmp_path):
    png = aa.timeline_png(_cfg(tmp_path), "2026-11-11", tl=TL, subs=SUBS)
    im = Image.open(io.BytesIO(png))
    assert im.format == "PNG" and im.width == 1400
    assert im.height > 46 + 4 * 34          # mount, rc16 + two sub rows
    assert aa.timeline_png(_cfg(tmp_path), "x", tl={"ok": False}, subs=[]) is None


def _rep(tmp_path):
    png = aa.timeline_png(_cfg(tmp_path), "2026-11-11", tl=TL, subs=SUBS)
    return {"date": "2026-11-11", "title": "AARO night 2026-11-11: 4 kept subs, 0.1 h",
            "lines": ["RC16: 2 kept / 3 rejected", "Piggy-600: 2 kept"], "kept": 4,
            "rejected": 3, "hours": 0.13, "timeline_png": png,
            "thumbs": [{"kind": "kept", "why": "best", "bytes": png, "caption": "RC16 NGC 604 L 120 s"}],
            "links": [{"target": "NGC 604", "label": "Progression sheet",
                       "url": "https://claude.ai/artifact/YTZwa8BcKopRU6zyWQzSDc"}],
            "url": "https://teles.example/report/2026-11-11"}


def test_page_and_email_carry_text_images_and_links(tmp_path):
    rep = _rep(tmp_path)
    page = aa.page_html(rep)
    assert page.count("data:image/png;base64,") == 2
    assert "Progression sheet" in page and "RC16: 2 kept / 3 rejected" in page
    cfg = _cfg(tmp_path, after_action_smtp_user="scope@example.com",
               after_action_email_to="me@example.com")
    msg = aa.email_message(cfg, rep)
    assert msg["Subject"] == rep["title"] and msg["To"] == "me@example.com"
    raw = msg.as_string()
    assert "cid:timeline" in raw and "Content-ID: <timeline>" in raw
    assert "Content-ID: <thumb0>" in raw and "YTZwa8BcKopRU6zyWQzSDc" in raw


def test_send_push_attaches_the_timeline_and_email_needs_smtp(tmp_path, monkeypatch):
    rep = _rep(tmp_path)
    monkeypatch.setattr(aa, "build", lambda cfg, date: rep)
    pushes, mails = [], []

    async def notify(cfg, msg, **kw):
        pushes.append((msg, kw))

    cfg = _cfg(tmp_path, after_action_mode="both")
    res = asyncio.run(aa.send(cfg, "2026-11-11", notify=notify,
                              smtp=lambda c, r: mails.append(r) or "email sent"))
    assert res["steps"] == ["push sent", "email sent"]
    msg, kw = pushes[0]
    assert msg.startswith("Report: https://teles.example/report/2026-11-11")
    assert kw["attachment"].endswith("2026-11-11_timeline.png")
    assert (tmp_path / "data" / "reports" / "2026-11-11_report.html").exists()
    assert "SMTP not set" in aa.send_email(_cfg(tmp_path), rep)


def test_due_once_per_night_after_the_shutdown(tmp_path):
    cfg = _cfg(tmp_path, after_action_after_shutdown_min=40)

    class A:
        shutdown = {"at": "2026-11-12T12:52:24Z"}
    at = datetime(2026, 11, 12, 12, 52, 24)
    assert aa.due(cfg, A, at + timedelta(minutes=30)) is None
    assert aa.due(cfg, A, at + timedelta(minutes=41)) == "2026-11-11"
    aa._mark(cfg, "2026-11-11", {"steps": ["push sent"]})
    assert aa.due(cfg, A, at + timedelta(minutes=50)) is None
    assert aa.due(cfg, A, at + timedelta(hours=13)) is None


def test_routes_are_defined_and_included():
    from pathlib import Path
    from photonscript.scheduler.routers import report
    paths = {getattr(r, "path", "") for r in report.router.routes}
    assert {"/report/{date}", "/api/runs/{date}/timeline.png",
            "/api/report/{date}/send"} <= paths
    src = (Path(report.__file__).parents[1] / "app.py").read_text(encoding="utf-8")
    assert "app.include_router(_report_router.router)" in src
    assert "from photonscript.scheduler.after_action import run_loop" in src
