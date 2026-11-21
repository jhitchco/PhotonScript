"""guide-rms-backfill: subs graded before the window-RMS fix kept the rolling
RMS at grading time (2026-10-12: a constant 34.84" on the RC16 subs while
PHD2 was stopped or LostLock). The pass recomputes each pre-fix sub's RMS
from the night's PHD2 guide log, or sets none with guide_rms_frames 0 where
PHD2 was not guiding; the scorecard row follows; human verdicts stay."""
import json
from pathlib import Path

from typer.testing import CliRunner

from photonscript.scheduler import guide_rms_backfill as grb
from photonscript.scheduler import phd2_analysis as pa
from photonscript.scheduler.runs import runs_dir
from photonscript.shared import qa_rules
from photonscript.shared.config import PhotonScriptConfig

NIGHT = "2026-10-12"
STALE = 34.84
COLS = ("Frame,Time,mount,dx,dy,RARawDistance,DECRawDistance,RAGuideDistance,"
        "DECGuideDistance,RADuration,RADirection,DECDuration,DECDirection,XStep,"
        "YStep,StarMass,SNR,ErrorCode")


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              image_watch_dir=str(tmp_path / "fits"),
                              phd2_logs_dir=str(tmp_path / "phd2"),
                              observatory_tz="America/Denver", **kw)


def _guide_log(tmp_path, ra_px=1.0, scale=0.5, minutes=30):
    """One guiding session 21:00-21:30 MDT (03:00-03:30Z) at ra_px px."""
    lines = ["PHD2 version 2.6.14 [Windows], Log version 2.5. Log enabled at "
             "2026-10-12 20:00:00", "",
             "Guiding Begins at 2026-10-12 21:00:00",
             f"Pixel scale = {scale:.2f} arc-sec/px, Binning = 2, Focal length = 3248 mm",
             "Exposure = 3000 ms",
             "Mount = Driver, connected, guiding enabled, xAngle = 90.3, xRate = 19.462, "
             "yAngle = 5.1, yRate = 33.238, parity = +/+",
             "Lock position = 149.550, 903.467, Star position = 149.550, 903.467, HFD = 3.0 px",
             COLS]
    for i in range(minutes * 20):
        lines.append(f'{i + 1},{3.0 * (i + 1):.3f},"Mount",0,0,{ra_px:.3f},0.000,'
                     "0,0,0,,0,,,,200000,40.0,0")
    lines.append("Guiding Ends at 2026-10-12 21:30:00")
    d = tmp_path / "phd2"
    d.mkdir(parents=True, exist_ok=True)
    (d / "PHD2_GuideLog_2026-10-12_200000.txt").write_text("\n".join(lines) + "\n",
                                                           encoding="utf-8")


def _rec(cfg, name, start, rms=STALE, gstate="guiding", **extra):
    r = {"rig": "rc16", "file": f"LIGHT/{name}.fits", "target": "Heart Nebula",
         "filter": "Ha", "exp_s": 300.0, "start_utc": start, "hfr": 3.0,
         "ecc": 0.3, "stars": 200, "background": 900, "ccd_temp": 0.0,
         "guide_rms": rms, "guide_state": gstate}
    card = qa_rules.evaluate(qa_rules.metrics_from_record(r),
                             qa_rules.context(cfg, "rc16", "Heart Nebula", "Ha"))
    r.update(card.record_fields())
    r.update(extra)
    return r


def _seed(cfg, recs):
    p = runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    p.write_text("".join(json.dumps(r) + "\n" for r in recs), encoding="utf-8")
    return p


def _load(cfg):
    p = runs_dir(cfg) / f"{NIGHT}_subs.jsonl"
    return {r["file"]: r for r in map(json.loads, p.read_text(encoding="utf-8").splitlines())}


def _row(rec):
    return next(x for x in qa_rules.expand(rec["scorecard"]) if x["id"] == "guide_rms")


def test_guided_sub_gets_its_log_rms_and_stopped_one_none(tmp_path):
    cfg = _cfg(tmp_path)
    _guide_log(tmp_path)
    _seed(cfg, [
        _rec(cfg, "a", "2026-10-13T03:05:00Z", gstate="lost_star"),  # inside the log
        _rec(cfg, "b", "2026-10-13T04:00:00Z", gstate="stopped"),    # PHD2 stopped
        _rec(cfg, "c", "2026-10-13T03:10:00Z", rms=0.61,             # post-fix record
             guide_rms_frames=95),
    ])
    before = (runs_dir(cfg) / f"{NIGHT}_subs.jsonl").read_text(encoding="utf-8")
    dry = grb.night_pass(cfg, NIGHT)                    # dry run by default
    assert (runs_dir(cfg) / f"{NIGHT}_subs.jsonl").read_text(encoding="utf-8") == before
    assert dry["guide_log"] and dry["pre_fix"] == 2 and dry["stale"] == 2
    assert dry["from_log"] == 1 and dry["nulled"] == 1 and dry["records_updated"] == 2
    ex = {x["file"]: x for x in dry["examples"]}
    assert ex["LIGHT/a.fits"]["new"] == 0.5 and ex["LIGHT/a.fits"]["old"] == STALE
    assert "[dry run]" in grb.format_line(dry)

    res = grb.night_pass(cfg, NIGHT, apply=True)
    assert res["records_updated"] == 2
    by = _load(cfg)
    a, b, c = by["LIGHT/a.fits"], by["LIGHT/b.fits"], by["LIGHT/c.fits"]
    assert a["guide_rms"] == 0.5 and a["guide_rms_frames"] == 101   # 03:05:00-03:10:00 inclusive
    assert a["guide_rms_src"] == "phd2_log"
    assert b["guide_rms"] is None and b["guide_rms_frames"] == 0
    assert b["guide_rms_src"] == "not_guiding"
    assert c["guide_rms"] == 0.61 and "guide_rms_src" not in c
    # the stored scorecard row follows the new value
    assert _row(a)["value"] == 0.5 and _row(b)["value"] is None
    # a second pass finds nothing left to do
    again = grb.night_pass(cfg, NIGHT, apply=True)
    assert again["pre_fix"] == 0 and again["records_updated"] == 0


def test_human_verdict_kept_auto_verdict_follows_in_fail_mode(tmp_path):
    cfg = _cfg(tmp_path, qa_guide_rms_mode="fail", quality_tracking_rms_max=1.5)
    _guide_log(tmp_path)
    auto = _rec(cfg, "auto", "2026-10-13T03:05:00Z")
    human = _rec(cfg, "human", "2026-10-13T03:12:00Z", manual_qa="reject",
                 reviewed=True, review_source="manual", passed_qa=False,
                 reason="manual: satellite")
    assert auto["passed_qa"] is False and "guide_rms" in auto["drivers"]
    _seed(cfg, [auto, human])
    res = grb.night_pass(cfg, NIGHT, apply=True)
    assert res["verdicts_changed"] == 1 and res["newly_passed"] == 1
    by = _load(cfg)
    assert by["LIGHT/auto.fits"]["passed_qa"] is True
    assert by["LIGHT/auto.fits"]["guide_rms"] == 0.5
    h = by["LIGHT/human.fits"]
    assert h["guide_rms"] == 0.5 and _row(h)["value"] == 0.5
    assert h["passed_qa"] is False and h["manual_qa"] == "reject"
    assert h["reason"] == "manual: satellite" and h["review_source"] == "manual"
    assert res["human_kept"] == 1


def test_without_a_guide_log_only_not_guiding_records_are_nulled(tmp_path):
    cfg = _cfg(tmp_path)
    _seed(cfg, [_rec(cfg, "lost", "2026-10-13T03:05:00Z", gstate="lost_star"),
                _rec(cfg, "guided", "2026-10-13T03:12:00Z", gstate="guiding")])
    res = grb.night_pass(cfg, NIGHT, apply=True, timeline=None)
    assert not res["guide_log"] and res["nulled"] == 1 and res["no_log_left"] == 1
    by = _load(cfg)
    assert by["LIGHT/lost.fits"]["guide_rms"] is None
    assert by["LIGHT/lost.fits"]["guide_rms_frames"] == 0
    assert by["LIGHT/guided.fits"]["guide_rms"] == STALE
    assert "guide_rms_frames" not in by["LIGHT/guided.fits"]
    assert "left (guiding per the record, no log)" in grb.format_line(res)


def test_window_needs_three_guided_frames(tmp_path):
    cfg = _cfg(tmp_path)
    _guide_log(tmp_path)
    tl = pa.night_timeline(cfg, NIGHT)
    from datetime import datetime
    # 2 s of overlap at the end of the session: one frame, no RMS
    rms, frames = grb.window_from_log(tl, datetime(2026, 10, 13, 3, 29, 58), 300, 3)
    assert rms is None and frames == 1
    rms, frames = grb.window_from_log(tl, datetime(2026, 10, 13, 3, 0, 0), 300, 3)
    assert rms == 0.5 and frames == 100


def test_cli_dry_run_by_default(tmp_path, monkeypatch):
    from photonscript import cli
    cfg = _cfg(tmp_path)
    _guide_log(tmp_path)
    p = _seed(cfg, [_rec(cfg, "a", "2026-10-13T03:05:00Z")])
    before = p.read_text(encoding="utf-8")
    monkeypatch.setattr(cli, "_config_for_repo", lambda *_a, **_k: cfg)
    out = CliRunner().invoke(cli.app, ["guide-rms-backfill", "--date", NIGHT,
                                       "--examples"])
    assert out.exit_code == 0, out.output
    assert "1 from the log" in out.output and "[dry run]" in out.output
    assert "34.84 -> 0.5" in out.output
    assert p.read_text(encoding="utf-8") == before
    out = CliRunner().invoke(cli.app, ["guide-rms-backfill", "--date", NIGHT, "--apply"])
    assert out.exit_code == 0, out.output
    assert _load(cfg)["LIGHT/a.fits"]["guide_rms"] == 0.5
