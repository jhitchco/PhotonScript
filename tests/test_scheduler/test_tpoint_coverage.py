"""TPoint coverage check (scheduler/tpoint_coverage.py).

The fixture is the real TPoint input TheSky64 10.5 wrote for the 99-sample
base run of 2026-10-14 (Paramount MX, :EQUAT), read on 2026-10-15 with
GET /api/thesky/tpoint/file?rel=TPoint%5CTPoint%20base%20run%20in.dat,
byte for byte (CRLF). Every sample is east of the meridian; M33 trailed
on both rigs after the flip that night.
"""
from __future__ import annotations

import asyncio
import os
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

from photonscript.scheduler import tpoint_coverage as tc
from photonscript.scheduler import tpoint_files as tf
from photonscript.scheduler import tpoint_watch as tw
from photonscript.scheduler import unguided_limits as ul
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "tpoint"
REAL_IN = FIX / "2026-10-15" / "TPoint base run in.dat"
OUTMOD = FIX / "2026-10-14" / "TPoint base run outmod.dat"
M33_RA_H, M33_DEC = 1.5642, 30.66


def _cfg(tmp_path, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("nina_logs_dir", str(tmp_path / "ninalogs"))
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"), **kw)


def _real_text() -> str:
    return REAL_IN.read_bytes().decode("latin-1")


def _cov(tmp_path=None, **kw):
    cfg = _cfg(tmp_path, **kw) if tmp_path else None
    return tc.analyze(_real_text(), cfg, file="TPoint\\TPoint base run in.dat")


# ------------------------------------------------------------- the real run

def test_real_run_is_east_only_with_every_west_cell_empty(tmp_path):
    raw = REAL_IN.read_bytes()
    assert b"\r\n" in raw and raw.startswith(b"!TheSky Version 10.5.0 Build 14139")
    c = _cov(tmp_path)
    assert c["ok"] and c["points"] == 99 and c["unparsed"] == 0
    assert c["equatorial"] and c["lat_deg"] == pytest.approx(31.9069, abs=1e-3)
    assert c["by_ha"] == {"east far": 46, "east": 53, "west": 0, "west far": 0}
    assert c["beyond_pole"] == 94 and c["under_pole"] == 5
    # the convention, inferred from the file: beyond the pole = east-looking
    assert c["convention"]["beyond_pole_side"] == "east"
    assert c["convention"]["source"].startswith("inferred: 81 of 81")
    # the 5 rows under the pole sit at the meridian (the other pier side)
    assert c["meridian_zone"] == 5 and c["cross"] == 5
    assert c["by_side"] == {"east": 94, "west": 0}
    assert c["overall"] == "east-only"
    west = [x for x in c["cells"] if x["side"] == "west"]
    assert all(x["n"] == 0 for x in west)
    assert all(x["verdict"] == "empty" for x in west if x["usable"])
    assert len(c["empty"]) == sum(1 for x in west if x["usable"]) == 7
    east = {x["label"]: x["n"] for x in c["cells"] if x["side"] == "east"}
    assert sum(east.values()) == 94
    assert east["east Dec 30-60"] == 12 and east["east far Dec 30-60"] == 19


def test_real_run_top_up_suggestion(tmp_path):
    c = _cov(tmp_path)
    tu = c["topup"]
    assert tu["by_side"]["west"] == 42 and tu["points"] == 43 and tu["cells"] == 8
    # the run's own pace from the LST column (99 points from LST 20:53 to 22:11)
    assert tu["pace_source"] == "this run's LST gaps"
    assert 40 <= tu["pace_s"] <= 55
    assert tu["minutes"] == round(43 * tu["pace_s"] / 60)
    w = next(x for x in tu["per_cell"] if x["label"] == "west Dec 30-60")
    assert w["add"] == 6 and w["n"] == 0 and w["alt"] >= 30 and 180 < w["az"] < 360
    text = tc.format_text(c)
    assert "EAST-ONLY" in text and "0 EMPTY" in text
    assert "top-up to 6 per usable cell: 43 points (east 1, west 42)" in text
    line = c["line"]
    assert line.startswith("TPoint coverage: east-only (99 points: east of the meridian 94, west 0")
    assert "west 7 empty of 7 cells" in line and "Top-up: 43 points" in line
    assert chr(0x2014) not in text + line          # no em dashes


def test_pace_falls_back_to_the_config_default(tmp_path):
    head = "\n".join(_real_text().splitlines()[:6])     # site line + 1 sample
    c = tc.analyze(head, _cfg(tmp_path, tpoint_coverage_pace_s=30.0))
    assert c["points"] == 1 and c["topup"]["pace_source"] == "default"
    assert c["topup"]["pace_s"] == 30.0


def test_thin_and_target_follow_the_config(tmp_path):
    c = _cov(tmp_path, tpoint_coverage_thin=10, tpoint_coverage_target=12)
    assert c["thin_n"] == 10 and c["target_n"] == 12
    assert "east Dec 60+" in c["thin"]          # 5 samples < 10


# ------------------------------------------------------------- synthetic

def _sexa(v: float, hours=False, width=2) -> str:
    sign = "-" if v < 0 else ("" if hours else "+")
    v = abs(v)
    d = int(v)
    m = int((v - d) * 60)
    s = (v - d - m / 60) * 3600
    return f"{sign}{d:0{width}d} {m:02d} {s:07.4f}"


def _row(ha: float, dec: float, lst: float) -> str:
    """One TheSky-style sample: east of the meridian through the pole."""
    ra = (lst - ha) % 24
    if ha < 0:
        sra, sdec = (ra + 12) % 24, (180 - dec if dec >= 0 else -180 - dec)
    else:
        sra, sdec = ra, dec
    return (f"{_sexa(ra, True)} {_sexa(dec)} {_sexa(sra, True)} {_sexa(sdec)} "
            f"{int(lst):2d} {(lst % 1) * 60:6.3f}")


def _synthetic(per_cell=6, sides=("east", "west")) -> str:
    lines = ["!synthetic", "Paramount MX", ":NODA", ":EQAT".replace("EQAT", "EQUAT"),
             "+31 54 25 2026 10 20 10.0 870.00 1300.00 0.50 0.5500 0.0065"]
    lst = 20.0
    centres = {"east far": -4.0, "east": -1.5, "west": 1.5, "west far": 4.0}
    decs = {"<0": -10.0, "0-30": 15.0, "30-60": 45.0, "60+": 72.0}
    for hb in tc.HA_BANDS:
        if hb[3] not in sides:
            continue
        for db in tc.DEC_BANDS:
            for i in range(per_cell):
                lst += 0.012
                lines.append(_row(centres[hb[0]] + 0.1 * i, decs[db[0]] + i, lst))
    return "\r\n".join(lines) + "\r\n"


def test_synthetic_full_sky_is_full_with_no_top_up(tmp_path):
    c = tc.analyze(_synthetic(), _cfg(tmp_path))
    assert c["overall"] == "full" and c["convention"]["beyond_pole_side"] == "east"
    assert c["by_side"]["east"] and c["by_side"]["west"] and not c["cross"]
    assert all(x["verdict"] in ("ok", "below") for x in c["cells"])
    assert c["topup"]["points"] == 0 and "no top-up needed" in tc.format_text(c)


def test_synthetic_west_only_and_partial(tmp_path):
    c = tc.analyze(_synthetic(sides=("west",)), _cfg(tmp_path))
    assert c["overall"] == "west-only"
    c = tc.analyze(_synthetic(per_cell=2), _cfg(tmp_path))
    assert c["overall"] == "partial" and c["thin"] and not c["empty"]


def test_empty_and_garbage_text():
    c = tc.analyze("hello\n", None)
    assert not c["ok"] and c["overall"] == "none"
    assert tc.format_text(c).startswith("TPoint coverage:")


# ------------------------------------------------------------- arm check

def _m33(w0="2026-10-15T04:00:00", w1="2026-10-15T11:30:00", guided=False):
    return NS(name="M33", ra_hours=M33_RA_H, dec_degrees=M33_DEC,
              window_start_utc=datetime.fromisoformat(w0),
              window_end_utc=datetime.fromisoformat(w1), start_guiding=guided)


def test_arm_warning_for_an_m33_window_crossing_the_meridian(tmp_path):
    cfg = _cfg(tmp_path)
    cov = tc.compact(_cov(tmp_path))
    rec = tc.window_check(cfg, cov, [_m33()])
    assert rec["warn"]
    t = rec["targets"][0]
    # 22:00 to 05:30 MDT: east until the transit near 01:15 MDT, then west
    assert 150 <= t["minutes_east"] <= 230 and 220 <= t["minutes_west"] <= 290
    assert t["minutes_uncovered"] == t["minutes_west"]
    assert {c["label"] for c in t["cells"]} <= {"west Dec 30-60", "west far Dec 30-60"}
    assert all(c["n"] == 0 for c in t["cells"])
    line = rec["line"]
    assert line.startswith("TPoint coverage east-only: M33 spends")
    assert "with no samples (after the flip, west of the meridian)" in line
    assert "Top-up west: TPoint run of about 43 points" in line
    # the east half of the night alone is covered: no warning
    rec = tc.window_check(cfg, cov, [_m33(w1="2026-10-15T06:45:00")])
    assert not rec["warn"] and "covered cells" in rec["line"]


def test_arm_check_with_full_coverage_or_none(tmp_path):
    cfg = _cfg(tmp_path)
    full = tc.compact(tc.analyze(_synthetic(), cfg))
    assert not tc.window_check(cfg, full, [_m33()])["warn"]
    rec = tc.window_check(cfg, None, [_m33()])
    assert not rec["warn"] and "unknown" in rec["line"]


def test_armer_check_records_and_alerts_once_a_night(tmp_path, monkeypatch):
    from photonscript.scheduler import armer as am
    cfg = _cfg(tmp_path)
    cov = tc.compact(_cov(tmp_path))
    monkeypatch.setattr(tc, "current", lambda c, refresh=True: cov)
    a = NS(config=cfg, plan={"night_of": "2026-10-14"}, _tpcov_alert=None,
           _tpcov_night=None)
    now = datetime(2026, 10, 15, 3, 30)
    am.Armer._tpoint_coverage_check(a, [_m33()], now)
    assert a.plan["tpoint_coverage"]["warn"] and a._tpcov_alert
    sent = []

    async def fake_notify(c, msg, **kw):
        sent.append((msg, kw.get("title")))
    monkeypatch.setattr(am, "notify", fake_notify)
    asyncio.run(am.Armer._send_tpcov_alert(a))
    assert sent and sent[0][1] == "PhotonScript TPoint coverage"
    # a re-dispatch the same night records again but does not push again
    am.Armer._tpoint_coverage_check(a, [_m33()], now)
    assert a._tpcov_alert is None
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import night_of, read_jsonl
    evs = [e for e in read_jsonl(events_path(cfg, night_of(cfg, now)))
           if e.get("kind") == "tpoint_coverage"]
    assert len(evs) == 1 and evs[0]["value"] == "warn"


# ------------------------------------------------------------- unguided limits

def _tbl():
    return {"rig": "rc16", "ladder": [300.0, 240.0, 180.0, 120.0],
            "regions": [], "dec_bands": [],
            "overall": {"key": "all", "label": "whole sky", "limit_s": 300.0,
                        "source": "measured", "confidence": "high", "n": 20}}


def test_unguided_limits_treat_empty_tpoint_cells_as_unknown(tmp_path):
    cfg = _cfg(tmp_path)
    cov = tc.compact(_cov(tmp_path))
    east = ul.plan_target(cfg, _tbl(), M33_RA_H, M33_DEC,
                          datetime(2026, 10, 15, 4), datetime(2026, 10, 15, 6, 30),
                          coverage=cov)
    assert east["limit_s"] == 300 and east["scope"] == "whole sky"
    full = ul.plan_target(cfg, _tbl(), M33_RA_H, M33_DEC,
                          datetime(2026, 10, 15, 4), datetime(2026, 10, 15, 11, 30),
                          coverage=cov)
    assert full["limit_s"] == 120 and full["scope"] == "TPoint uncovered"
    assert full["side"] == "west" and full["source"] == "fallback"
    assert "TPoint has no samples in west Dec 30-60" in full["why"]
    assert "no learned limit is carried in from other regions" in full["why"]
    # without coverage: the whole-sky limit as before
    old = ul.plan_target(cfg, _tbl(), M33_RA_H, M33_DEC,
                         datetime(2026, 10, 15, 4), datetime(2026, 10, 15, 11, 30))
    assert old["limit_s"] == 300
    # configured fallback, and off
    cfg2 = _cfg(tmp_path, unguided_limit_uncovered_s=180)
    assert ul.plan_target(cfg2, _tbl(), M33_RA_H, M33_DEC, datetime(2026, 10, 15, 9),
                          datetime(2026, 10, 15, 11), coverage=cov)["limit_s"] == 180
    cfg3 = _cfg(tmp_path, unguided_limit_uncovered_s=-1)
    assert ul.plan_target(cfg3, _tbl(), M33_RA_H, M33_DEC, datetime(2026, 10, 15, 9),
                          datetime(2026, 10, 15, 11), coverage=cov)["limit_s"] == 300


def test_unguided_limits_keep_a_limit_measured_in_the_same_region(tmp_path):
    cfg = _cfg(tmp_path)
    cov = tc.compact(_cov(tmp_path))
    tbl = _tbl()
    k = ul.region_key(M33_DEC, 2.0)            # Dec +30..+50, west (1..3 h)
    tbl["regions"] = [{"key": k, "label": "Dec +30..+50, west", "limit_s": 240.0,
                       "dec_band": [30, 50], "ha_band": [1.0, 3.0],
                       "source": "measured", "confidence": "medium", "n": 6}]
    r = ul.plan_target(cfg, tbl, M33_RA_H, M33_DEC, datetime(2026, 10, 15, 8, 30),
                       datetime(2026, 10, 15, 9), coverage=cov)
    assert r["limit_s"] == 240 and r["scope"] == "region (TPoint cell empty)"
    assert "(TPoint has no samples in west Dec 30-60)" in r["why"]


def test_unguided_regions_map_onto_tpoint_cells(tmp_path):
    cov = tc.compact(_cov(tmp_path))
    row = {"dec_band": [30, 50], "ha_band": [1.0, 3.0]}
    assert ul.region_coverage(cov, row) == "empty"
    assert ul.region_coverage(cov, {"dec_band": [30, 50], "ha_band": [-1.0, 1.0]}) == "partial"
    assert ul.region_coverage(cov, {"dec_band": [30, 50], "ha_band": [-3.0, -1.0]}) == "covered"
    # Dec +50..+70 spans the 30-60 and 60+ cells; HA -12..-3 the east far band
    cs = tc.region_cells(cov, 50, 70, -12, -3)
    assert {c["key"] for c in cs} == {"east|east far|30-60", "east|east far|60+"}
    assert ul.region_coverage(None, row) is None
    doc = {"refreshed_at": "x", "rigs": {"rc16": {**_tbl(), "regions": [
        {"key": "k", "label": "Dec +30..+50, west", "n": 4, "limit_s": 180.0,
         "dec_band": [30, 50], "ha_band": [1.0, 3.0], "lengths": {},
         "source": "measured", "confidence": "medium"}]}}}
    text = ul.format_table(doc, coverage=cov)
    assert "[TPoint empty]" in text and "TPoint coverage east-only" in text


def test_apply_targets_records_the_coverage(tmp_path):
    cfg = _cfg(tmp_path, unguided_auto_length="advise")
    cov = tc.compact(_cov(tmp_path))
    rec = ul.apply_targets(cfg, [], tbl={"rigs": {"rc16": _tbl()}}, coverage=cov)
    assert rec["tpoint_coverage"] == "east-only"


# ------------------------------------------------------------- watcher, files

def _tree(tmp_path) -> Path:
    root = tmp_path / "Software Bisque" / "TheSky Professional Edition 64"
    tp = root / "TPoint"
    tp.mkdir(parents=True)
    now = time.time()
    for src, name in ((REAL_IN, "TPoint base run in.dat"),
                      (OUTMOD, "TPoint base run outmod.dat")):
        (tp / name).write_bytes(src.read_bytes())
        os.utime(tp / name, (now - 7200, now - 7200))
    return root


def test_stats_name_the_data_path_and_coverage_from_stats(tmp_path):
    cfg = _cfg(tmp_path, thesky_user_dir=str(_tree(tmp_path)))
    st = tf.stats(cfg)
    assert st["data_path"].endswith("TPoint base run in.dat")
    cov = tc.from_stats(cfg, st)
    assert cov["overall"] == "east-only" and cov["points"] == 99
    saved = tc.saved(cfg)
    assert saved["overall"] == "east-only" and saved["topup_points"] == 43
    assert saved["grid"][0]["dec_band"] == "60+"
    assert tc.morning_line(cfg).startswith("TPoint coverage: east-only")
    brief = tw.coverage_brief(cfg, st)
    assert brief["overall"] == "east-only" and brief["by_side"]["west"] == 0


def test_watcher_history_and_finish_text_carry_the_coverage(tmp_path):
    cfg = _cfg(tmp_path, thesky_user_dir=str(_tree(tmp_path)))
    lst = tf.list_files(cfg)
    rec = tw.summarize(cfg, tf.stats(cfg, lst), lst)
    assert rec["coverage"]["overall"] == "east-only"
    run = {"points": 99, "pace_s": 47.0, "finish_reason": "fitted",
           "finished_utc": "2026-10-15T04:20:00Z"}
    text = tw.finish_text(cfg, run, rec, None, None)
    assert "TPoint coverage: east-only" in text and "Top-up: 43 points" in text
    short = tw.short_line(cfg, run, rec, None, None)
    assert "coverage east-only (top-up 43 points" in short
    # the idle tick records the model and saves the coverage for the page
    res = tw.step(cfg, now=datetime.utcnow(), listing=lst, state={})
    assert res["state"]["coverage"]["overall"] == "east-only"
    assert "coverage east-only" in res["state"]["text"]
    assert tw.history(cfg)[-1]["coverage"]["overall"] == "east-only"


def test_routes_and_cli_show_the_coverage(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import thesky as rt
    cfg = _cfg(tmp_path, thesky_user_dir=str(_tree(tmp_path)))
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    st = asyncio.run(rt.api_thesky_tpoint())
    assert st["coverage"]["overall"] == "east-only" and "data_path" not in st
    assert "EAST-ONLY" in st["coverage_text"] and "EAST-ONLY" in st["text"]
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    r = CliRunner().invoke(cli.app, ["tpoint-stats"])
    assert r.exit_code == 0 and "EAST-ONLY" in r.output and "top-up to 6" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-stats", "--json"])
    assert r.exit_code == 0 and '"overall": "east-only"' in r.output


def test_morning_card_line(tmp_path, monkeypatch):
    from photonscript.scheduler import morning_report as mr
    cfg = _cfg(tmp_path)
    tc.save(cfg, _cov(tmp_path))
    card = {"date": "2026-10-14", "tpoint_coverage": tc.morning_line(cfg), "rigs": []}
    assert any(str(x).startswith("TPoint coverage: east-only")
               for x in mr.card_lines(card))
    tc.save(cfg, tc.analyze(_synthetic(), cfg))
    assert tc.morning_line(cfg) is None
