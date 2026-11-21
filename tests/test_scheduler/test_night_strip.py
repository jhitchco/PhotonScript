"""Night strip: the dashboard "Tonight so far" strip and the RC16 Roof row.

night_timeline.tonight_strip(config) reuses the PS-67 timeline rows (NINA
instruction per rig, sub spans) and the PS-71 safety history, collapses the
rig states to exposing / busy / idle, makes a Roof row (open / closed /
unknown), clips everything to sunset .. min(now, sunrise) and adds the
summary numbers. GET /api/tonight/strip serves it (sync handler, worker
thread, short shared cache). The dashboard draws it under the sun chart and
the RC16 equipment card gets the same Roof row as the Piggy-600 card.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import night_timeline as nt
from photonscript.shared import night_events
from tests.test_scheduler.test_ps67_pointing import _cfg

ROOT = Path(__file__).resolve().parents[2] / "photonscript" / "scheduler"
DASH = ROOT / "templates" / "dashboard.html"
RUNS = ROOT / "templates" / "runs.html"

NIGHT = "2026-10-11"
SUNSET = datetime(2026, 10, 12, 0, 48)
DUSK = datetime(2026, 10, 12, 2, 12)
DAWN = datetime(2026, 10, 12, 11, 57)
SUNRISE = datetime(2026, 10, 12, 13, 20)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(nt, "_night_window", lambda c, d: {
        "sunset": SUNSET, "astro_dusk": DUSK, "astro_dawn": DAWN,
        "sunrise": SUNRISE})
    return _cfg(tmp_path, observatory_tz="UTC")


def _hist(cfg, *rows):
    p = Path(cfg.data_dir) / "safety_history.jsonl"
    p.write_text("".join(json.dumps({"ts": t.isoformat() + "Z", "state": s,
                                     "source": "armer"}) + "\n"
                         for t, s in rows), encoding="utf-8")


def _sub(cfg, rig, t, exp=300, passed=True, reviewed=False):
    from photonscript.scheduler.runs import append_sub_record
    append_sub_record(cfg, NIGHT, {"rig": rig, "file": f"{rig}/{t:%H%M%S}.fits",
                                   "time": t.isoformat(), "exp_s": exp,
                                   "target": "M31", "filter": "L",
                                   "passed_qa": passed, "reviewed": reviewed})


def _rows(out):
    return {r["id"]: r for r in out["rows"]}


# ------------------------------------------------------------ the states

@pytest.mark.parametrize("state,strip", [
    ("exposing", "exposing"), ("autofocus", "busy"), ("centering", "busy"),
    ("slewing", "busy"), ("meridian flip", "busy"), ("dither", "busy"),
    ("flats", "busy"), ("other", "busy"), ("waiting", "idle"),
    ("parked", "idle"), ("tracking", "idle"), (None, "idle")])
def test_strip_state(state, strip):
    assert nt.strip_state(state) == strip


def test_busy_states_collapse_into_one_run():
    t = SUNSET + timedelta(hours=2)
    m = timedelta(minutes=1)
    segs = [{"start": t, "end": t + 2 * m, "state": "slewing"},
            {"start": t + 2 * m, "end": t + 3 * m, "state": "centering"},
            {"start": t + 3 * m, "end": t + 6 * m, "state": "autofocus"},
            {"start": t + 6 * m, "end": t + 30 * m, "state": "exposing"},
            {"start": t + 30 * m, "end": t + 31 * m, "state": "dither"},
            {"start": t + 31 * m, "end": t + 40 * m, "state": "exposing"},
            {"start": t + 40 * m, "end": t + 50 * m, "state": "waiting"}]
    out = nt._collapse(segs)
    assert [s["state"] for s in out] == ["busy", "exposing", "busy",
                                         "exposing", "idle"]
    assert out[0]["label"] == "slewing, centering, autofocus"
    assert out[0]["end"] == t + 6 * m
    assert out[1]["label"] is None and out[2]["label"] == "dither"
    assert out[4]["label"] == "waiting"


# ---------------------------------------------------------------- roof

def test_roof_from_safety_history_carries_the_earlier_state(cfg):
    _hist(cfg, (SUNSET - timedelta(hours=10), "unsafe"),     # closed at dawn
          (SUNSET + timedelta(hours=2), "safe"),
          (SUNSET + timedelta(hours=4), "unsafe"),
          (SUNSET + timedelta(hours=4, minutes=30), "safe"),
          (SUNSET + timedelta(hours=9), "unsafe"))             # after now
    now = SUNSET + timedelta(hours=5)
    _sub(cfg, "rc16", SUNSET + timedelta(hours=3))
    out = nt.tonight_strip(cfg, NIGHT, now=now)
    roof = _rows(out)["roof"]
    assert roof["source"] == "history"
    assert [s["state"] for s in roof["segments"]] == ["closed", "open",
                                                      "closed", "open"]
    assert roof["segments"][0]["start"] == "2026-10-12T00:48:00Z"
    assert roof["segments"][-1]["end"] == "2026-10-12T05:48:00Z"   # now
    assert out["summary"]["roof_open_s"] == 2 * 3600 + 30 * 60
    assert out["summary"]["elapsed_s"] == 5 * 3600


def test_roof_unknown_without_history_and_fallback_marks_only_closed(cfg):
    a, b = nt.roof_segments(cfg, SUNSET, SUNSET + timedelta(hours=1))
    assert [s["state"] for s in a] == ["unknown"] and b == "none"
    audit = Path(cfg.data_dir) / "notifications.jsonl"
    from photonscript.shared.pushover import _audit_path
    audit = _audit_path(cfg)
    audit.parent.mkdir(parents=True, exist_ok=True)
    audit.write_text("".join(json.dumps({"ts": (SUNSET + timedelta(minutes=m))
                                         .isoformat() + "Z", "title": t}) + "\n"
                             for m, t in ((10, "PhotonScript paused"),
                                          (40, "PhotonScript resumed"))),
                     encoding="utf-8")
    segs, src = nt.roof_segments(cfg, SUNSET, SUNSET + timedelta(hours=1))
    assert src == "notifications"
    assert [s["state"] for s in segs] == ["unknown", "closed", "unknown"]


# ------------------------------------------------------- rigs + clipping

def test_rig_rows_from_nina_events_clipped_to_now(cfg):
    t = SUNSET + timedelta(hours=1, minutes=30)
    ev = night_events.EventLog(cfg, "rc16")
    ev.change("nina", "instruction", "Slew and center", now=t)
    ev.change("nina", "instruction", "Run Autofocus", now=t + timedelta(minutes=3))
    ev.change("nina", "instruction", "Smart Exposure", now=t + timedelta(minutes=8))
    _sub(cfg, "rc16", t + timedelta(minutes=8), passed=True, reviewed=True)
    _sub(cfg, "rc16", t + timedelta(minutes=14), passed=False)
    _sub(cfg, "rc16", t + timedelta(minutes=20))
    _sub(cfg, "rc16", t + timedelta(hours=3))          # after now: not shown
    now = t + timedelta(minutes=40)
    out = nt.tonight_strip(cfg, NIGHT, now=now)
    assert out["started"] is True and out["upto"] == "2026-10-12T02:58:00Z"
    rc = _rows(out)["rc16"]
    assert rc["source"] == "NINA sequence"
    assert [s["state"] for s in rc["segments"]] == ["busy", "exposing"]
    assert rc["segments"][0]["label"] == "centering, autofocus"
    assert rc["segments"][-1]["end"] <= out["upto"]
    for row in out["rows"]:
        for s in row["segments"]:
            assert out["t0"] <= s["start"] < s["end"] <= out["upto"]
    tk = out["ticks"]["rc16"]
    assert [x["kept"] for x in tk] == [True, False, True]
    sm = out["summary"]
    assert sm["subs"] == 3 and sm["kept"] == 2
    assert sm["rigs"]["rc16"]["exposing_s"] > 0
    assert sm["rigs"]["rc16"]["busy_s"] == 8 * 60


def test_piggy_without_nina_log_uses_sub_spans(cfg):
    t = SUNSET + timedelta(hours=2)
    for k in range(3):
        _sub(cfg, "piggyback", t + timedelta(minutes=5 * k), exp=300)
    _sub(cfg, "piggyback", t + timedelta(minutes=40), exp=300, passed=False)
    out = nt.tonight_strip(cfg, NIGHT, now=t + timedelta(hours=1))
    pg = _rows(out)["piggyback"]
    assert pg["source"] == "sub spans only" and pg["label"] == "Piggy-600"
    assert [s["state"] for s in pg["segments"]] == ["exposing", "exposing"]
    assert out["summary"]["rigs"]["piggyback"]["exposing_s"] == 20 * 60
    assert out["summary"]["rigs"]["piggyback"]["kept"] == 3
    assert _rows(out)["rc16"]["segments"] == []


def test_after_sunrise_the_strip_ends_at_sunrise(cfg):
    _sub(cfg, "rc16", SUNSET + timedelta(hours=3))
    out = nt.tonight_strip(cfg, NIGHT, now=SUNRISE + timedelta(hours=2))
    assert out["upto"] == "2026-10-12T13:20:00Z"
    assert out["summary"]["elapsed_s"] == int((SUNRISE - SUNSET).total_seconds())
    assert _rows(out)["roof"]["segments"][-1]["end"] == out["upto"]


def test_empty_night_and_before_sunset(cfg):
    early = nt.tonight_strip(cfg, NIGHT, now=SUNSET - timedelta(hours=1))
    assert early["started"] is False and early["rows"] == []
    assert early["note"] == "tonight starts at sunset"
    out = nt.tonight_strip(cfg, NIGHT, now=SUNSET + timedelta(hours=2))
    assert out["started"] is False and out["note"] == "no run yet tonight"
    assert out["summary"]["subs"] == 0
    assert [r["id"] for r in out["rows"]] == ["roof", "rc16", "piggyback"]
    assert all(r["segments"] == [] for r in out["rows"][1:])
    assert out["t0"] == "2026-10-12T00:48:00Z" and out["t1"] == "2026-10-12T13:20:00Z"
    assert out["dark_s"] == int((DAWN - DUSK).total_seconds())


def test_runs_timeline_unchanged_by_raw_flag(cfg):
    _sub(cfg, "rc16", SUNSET + timedelta(hours=3))
    tl = nt.timeline(cfg, NIGHT)
    raw = nt.timeline(cfg, NIGHT, raw=True)
    assert isinstance(tl["t0"], str) and isinstance(raw["t0"], datetime)
    assert tl["subs"]["rc16"][0]["start"].endswith("Z")


# ------------------------------------------------------------- endpoint

def test_endpoint_is_sync_cached_and_read_only(cfg, monkeypatch):
    import inspect
    import photonscript.scheduler.app as app
    from photonscript.scheduler.routers import pointing as prouter
    monkeypatch.setattr(app, "_config", cfg)
    assert not inspect.iscoroutinefunction(prouter.api_tonight_strip)
    calls = []
    real = nt.tonight_strip
    monkeypatch.setattr(nt, "tonight_strip",
                        lambda c, d=None: calls.append(d) or real(c, d or NIGHT,
                                                                  now=SUNSET))
    prouter._strip_cache.clear()
    a = prouter.api_tonight_strip()
    b = prouter.api_tonight_strip()
    assert a is b and calls == [None]
    prouter.api_tonight_strip(date=NIGHT)            # explicit date: uncached
    assert calls == [None, NIGHT]
    prouter._strip_cache.clear()
    paths = {(r.path, tuple(sorted(r.methods))) for r in prouter.router.routes
             if getattr(r, "path", "") == "/api/tonight/strip"}
    assert paths == {("/api/tonight/strip", ("GET",))}


# ------------------------------------------------------------- template

def _strip_js():
    html = DASH.read_text(encoding="utf-8")
    a = html.index("// Night strip:")
    b = html.index("// end night strip")
    return html, html[a:b]


def test_dashboard_has_the_strip_under_the_sun_chart():
    html, js = _strip_js()
    i_sun, i_strip, i_tl = (html.index('id="sunCard"'), html.index('id="tonightStrip"'),
                            html.index('id="nightTimeline"'))
    assert i_sun < i_strip < i_tl
    assert "/api/tonight/strip" in js
    # one poll, once a minute, paused while the tab is hidden
    assert html.count("setInterval(whenShown(loadTonightStrip), 60 * 1000)") == 1
    # three rows only; the Runs legend colors
    for c in ("#2563eb", "#16a34a", "#b91c1c"):
        assert c in js
    assert "#2563eb" in RUNS.read_text(encoding="utf-8")
    for word in ("exposing", "busy", "idle", "Roof open", "subs", "kept"):
        assert word in js
    assert "<title>" in js            # hover tooltips with start-end times
    assert "no run yet tonight" in js or "d.note" in js
    assert js.isascii()


def test_rc16_card_gets_the_roof_row():
    html = DASH.read_text(encoding="utf-8")
    a = html.index("// Roof (both rigs")
    b = html.index("// PS-189: Wanderer Cover")
    block = html[a:b]
    assert "!mnt && sm" not in block
    assert "if (sm)" in block
    assert "roof open (safe)" in block and "roof closed (unsafe)" in block
    # the Piggy-only hints stay on the Piggy card
    assert "can do darks/bias" in block and "r.rig === 'rc16'" in block
    assert block.isascii()


def test_sub_spans_bridge_short_download_gaps(cfg):
    t = SUNSET + timedelta(hours=2)
    for k in range(4):       # 300 s subs every 6 min: 60 s gaps
        _sub(cfg, "piggyback", t + timedelta(minutes=6 * k), exp=300)
    _sub(cfg, "piggyback", t + timedelta(minutes=40), exp=300)   # 17 min gap
    out = nt.tonight_strip(cfg, NIGHT, now=t + timedelta(hours=1))
    segs = _rows(out)["piggyback"]["segments"]
    assert len(segs) == 2
    assert segs[0]["start"] == "2026-10-12T02:48:00Z"
    assert segs[0]["end"] == "2026-10-12T03:11:00Z"
    assert out["summary"]["rigs"]["piggyback"]["subs"] == 5
