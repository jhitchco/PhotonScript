"""TPoint run watcher, fitted-model history and the unguided limits epoch.

fixtures/tpoint/2026-10-14/ are the two files TheSky64 autosaved mid-run on
2026-10-14 (the 228-point calibration run after the SharpCap polar
alignment), read with GET /api/thesky/tpoint/file?rel= at 89 points: 6
basic terms, sky RMS 112.53", MA -75.8", ME +7.6". The previous model is
the 2026-10-10 base model (337 points in the fit, RMS 19.94", 32 terms,
MA -160.1", ME -79.6") from fixtures/tpoint/2026-10-10/.
"""
import asyncio
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import tpoint_files as tf
from photonscript.scheduler import tpoint_watch as tw
from photonscript.shared.config import PhotonScriptConfig

FIX = Path(__file__).parent / "fixtures" / "tpoint"
RUN_IN = FIX / "2026-10-14" / "TPoint base run in.dat"
RUN_OUT = FIX / "2026-10-14" / "TPoint base run outmod.dat"
OLD_BASE = FIX / "2026-10-10" / "TPoint base run outmod.dat"


def _cfg(tmp_path, root, **kw):
    kw.setdefault("thesky_tcp_host", "127.0.0.1")
    kw.setdefault("thesky_tcp_port", 1)
    kw.setdefault("nina_logs_dir", str(tmp_path / "ninalogs"))
    kw.setdefault("unguided_auto_length", "off")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path / "data"),
                              thesky_user_dir=str(root), **kw)


def _put(path: Path, data: bytes, age_s: float) -> None:
    path.write_bytes(data)
    t = time.time() - age_s
    os.utime(path, (t, t))


def _first_points(n: int) -> bytes:
    """The run's in.dat as it was after n points."""
    lines = RUN_IN.read_bytes().decode("latin-1").splitlines()
    head = 5                          # version, caption, 2 options, site line
    return "\r\n".join(lines[:head + n] + [""]).encode("latin-1")


def _tree(tmp_path, age_s: float = 60, n: int | None = None) -> Path:
    """TheSky's TPoint folder mid-run: the run's in.dat / outmod.dat and
    the previous model as Super Model Outmod.dat (4 days old)."""
    root = tmp_path / "TheSky Professional Edition 64"
    tp = root / "TPoint"
    tp.mkdir(parents=True, exist_ok=True)
    _put(tp / "Super Model Outmod.dat", OLD_BASE.read_bytes(), 4 * 86400)
    _put(tp / "TPoint base run in.dat",
         RUN_IN.read_bytes() if n is None else _first_points(n), age_s)
    _put(tp / "TPoint base run outmod.dat", RUN_OUT.read_bytes(), age_s)
    return root


def _now():
    return datetime.utcnow()


# ------------------------------------------------------------- parsing

def test_observations_and_pace_from_the_lst_column():
    lst, first = tw.observations(RUN_IN.read_bytes().decode("latin-1"))
    assert len(lst) == 89 == tf.parse_data(RUN_IN.read_text(encoding="latin-1"))["points"]
    assert first.startswith("01 27 02.8558 +82 59 47.551")
    assert lst[0] == pytest.approx(20 + 53.173 / 60)
    p = tw.pace_s(lst)
    assert 40 < p < 50                                     # about 45 s a point
    assert tw.pace_s([23.99, 0.005]) == pytest.approx(53.9, abs=0.1)   # LST wraps (54 sidereal s)
    assert tw.pace_s([1.0, 2.0]) is None                   # a 1 h pause is no pace


def test_the_running_file_is_the_newest_raw_input():
    lst = {"files": [{"rel": "TPoint\\TPoint base run outmod.dat"},
                     {"rel": "TPoint\\Super Model Outdat.dat"},
                     {"rel": "TPoint\\TPoint base run in.dat"}]}
    assert tw.run_file(lst)["rel"].endswith("in.dat")
    assert tw.run_file({"files": [{"rel": "TPoint\\Super Model Indat.dat"}]})
    assert tw.run_file({"files": [{"rel": "TPoint\\x outmod.dat"}]}) is None


# ------------------------------------------------------------- the run

def test_run_progress_stop_and_finish_once(tmp_path):
    root = _tree(tmp_path)
    cfg = _cfg(tmp_path, root, tpoint_run_target_points=228)
    now = _now()
    s = tw.step(cfg, now)["state"]
    assert s["status"] == "running" and s["run"]["points"] == 89
    assert s["run"]["target_points"] == 228 and s["run"]["eta_utc"]
    assert "89 points of 228" in s["text"] and "s/point" in s["text"] and "ETA" in s["text"]
    assert s["baseline"]["model_file"].endswith("Super Model Outmod.dat")
    assert s["baseline"]["terms"]["MA"] == -160.1
    r = tw.step(cfg, now + timedelta(minutes=20))
    assert r["state"]["status"] == "stopped" and r["push"] is None
    assert "waiting for the model fit" in r["state"]["text"]
    r = tw.step(cfg, now + timedelta(minutes=70))
    assert r["state"]["status"] == "finished"
    text = r["push"]
    assert text.startswith("TPoint run finished: 89 points (target 228)")
    assert "basic terms only" in text
    assert "sky RMS 112.53\" (was 19.94\")" in text
    assert "terms fitted 6 (was 32)" in text
    assert "MA -75.8\" (was -160.1\"), ME +7.6\" (was -79.6\")" in text
    assert "polar 1.87' (was 2.67')" in text
    assert "evidence restarts" in text
    # history: the previous model then this one; the epoch moved
    h = tw.history(cfg)
    assert [x["sky_rms_arcsec"] for x in h] == [19.9378, 112.5327]
    assert h[1]["terms"] == {"IH": -4248.7, "ID": -2771.3, "MA": -75.8, "ME": 7.6,
                             "CH": -144.8, "NP": -106.3}
    ep = tw.load_epoch(cfg)
    assert ep["since"] == h[1]["night"] and ep["d_ma_arcsec"] == pytest.approx(84.2, abs=0.1)
    # never pushed twice, nothing new recorded
    r = tw.step(cfg, now + timedelta(minutes=80))
    assert r["push"] is None and len(tw.history(cfg)) == 2
    # the morning report line (a TPoint night has no subs log: an earlier date)
    line = tw.morning_line(cfg, "2000-01-01")
    assert line.startswith("TPoint: 89-point run finished") and "(was 19.94\")" in line
    assert line.count("evidence restarts") == 1
    assert tw.morning_line(cfg, "2999-01-01") is None


def test_a_fitted_model_finishes_the_run_at_once(tmp_path):
    root = _tree(tmp_path, n=60)
    cfg = _cfg(tmp_path, root)
    now = _now()
    s = tw.step(cfg, now)["state"]
    assert s["status"] == "running" and s["run"]["points"] == 60
    assert "ETA" not in s["text"] and "s/point" in s["text"]     # no target: pace only
    tp = root / "TPoint"
    _put(tp / "TPoint base run in.dat", RUN_IN.read_bytes(), 30)
    s = tw.step(cfg, now + timedelta(seconds=60))["state"]
    assert s["status"] == "running" and s["run"]["points"] == 89
    # TPoint's Super Model fit writes the run's model with more than 6 terms
    fitted = OLD_BASE.read_bytes()
    _put(tp / "TPoint base run outmod.dat", fitted, 0)
    r = tw.step(cfg, now + timedelta(seconds=120))
    assert r["state"]["status"] == "finished" and r["state"]["run"]["finish_reason"] == "fitted"
    assert "(model fitted)" in r["push"] and "terms fitted 32 (was 32)" in r["push"]


def test_a_run_seen_after_it_ended_is_not_pushed_but_seeds_the_history(tmp_path):
    root = _tree(tmp_path, age_s=6 * 3600)
    cfg = _cfg(tmp_path, root)
    r = tw.step(cfg, _now())
    assert r["state"]["status"] == "idle" and r["push"] is None
    h = tw.history(cfg)
    assert len(h) == 2 and h[0]["source"] == "files (before this run)"
    assert tw.load_epoch(cfg)["since"] == h[1]["night"]
    assert "TPoint model" in r["state"]["text"] and "MA -75.8\"" in r["state"]["text"]
    # the same files again: nothing added
    tw.step(cfg, _now() + timedelta(minutes=5))
    assert len(tw.history(cfg)) == 2


def test_a_paused_run_compares_with_the_model_before_it(tmp_path):
    # first seen 20 min after its last point (idle: the history records
    # that partial model), then it grows again and finishes
    root = _tree(tmp_path, age_s=20 * 60, n=60)
    cfg = _cfg(tmp_path, root)
    now = _now()
    assert tw.step(cfg, now)["state"]["status"] == "idle"
    assert len(tw.history(cfg)) == 2
    _put(root / "TPoint" / "TPoint base run in.dat", RUN_IN.read_bytes(), 0)
    assert tw.step(cfg, now + timedelta(seconds=30))["state"]["status"] == "running"
    r = tw.step(cfg, now + timedelta(hours=2))
    assert r["state"]["status"] == "finished"
    assert "(was 19.94\")" in r["push"] and "MA -75.8\" (was -160.1\")" in r["push"]


def test_tick_pushes_in_alert_mode_only(tmp_path):
    sent = []

    async def notify(cfg, msg, **kw):
        sent.append((msg, kw))
        return True
    for mode, n in (("observe", 0), ("alert", 1)):
        root = _tree(tmp_path / mode)
        cfg = _cfg(tmp_path / mode, root, tpoint_watch_mode=mode)
        now = _now()
        asyncio.run(tw.tick(cfg, now, notify=notify))
        asyncio.run(tw.tick(cfg, now + timedelta(hours=2), notify=notify))
        assert len(sent) == n
    assert sent[0][1]["title"] == "PhotonScript TPoint run finished"
    cfg = _cfg(tmp_path / "off", _tree(tmp_path / "off"), tpoint_watch_mode="off")
    assert asyncio.run(tw.tick(cfg))["state"]["status"] == "off"
    assert not tw.state_path(cfg).exists()


# ------------------------------------------------------------- epoch

def _rec(ma, me, kind="base", f="a", night="2026-10-14"):
    return {"model_file": f, "model_mtime": f, "kind": kind, "night": night,
            "model_date": night, "terms": {"MA": ma, "ME": me}}


def test_epoch_only_for_a_new_base_model_with_a_moved_axis(tmp_path):
    cfg = _cfg(tmp_path, tmp_path / "none")
    now = _now()
    assert tw.check_epoch(cfg, _rec(-150.0, -70.0, f="b"), _rec(-160.0, -79.6), now) is None
    assert tw.check_epoch(cfg, _rec(-100.0, -79.6, kind="recal", f="b"),
                          _rec(-160.0, -79.6), now) is None
    assert tw.check_epoch(cfg, _rec(-100.0, -79.6), _rec(-160.0, -79.6), now) is None  # same model
    ep = tw.check_epoch(cfg, _rec(-100.0, -79.6, f="b"), _rec(-160.0, -79.6), now)
    assert ep["since"] == "2026-10-14" and "MA +60\"" in ep["note"]
    assert tw.epoch_since(cfg) == "2026-10-14"
    # an older night never moves it back
    assert tw.check_epoch(cfg, _rec(0.0, 0.0, f="c", night="2026-10-01"),
                          _rec(-160.0, -79.6), now) is None
    cfg0 = _cfg(tmp_path / "z", tmp_path / "none", tpoint_epoch_polar_shift_arcsec=0)
    assert tw.check_epoch(cfg0, _rec(-100.0, -79.6, f="b"), _rec(-160.0, -79.6), now) is None


def test_unguided_limits_since_uses_the_later_epoch(tmp_path, monkeypatch):
    from photonscript.scheduler import tracking_drift
    from photonscript.scheduler import unguided_limits as ul
    cfg = _cfg(tmp_path, tmp_path / "none")
    monkeypatch.setattr(tracking_drift, "tpoint_model",
                        lambda c, n: {"model_date": "2026-10-10"})
    assert ul.since_date(cfg, "2026-10-15") == "2026-10-10"
    tw._write_json(tw.epoch_path(cfg), {"since": "2026-10-14"})
    assert ul.since_date(cfg, "2026-10-15") == "2026-10-14"
    assert ul.since_date(cfg, "2026-10-12") == "2026-10-10"   # an earlier night
    monkeypatch.setattr(tracking_drift, "tpoint_model",
                        lambda c, n: {"model_date": "2026-10-20"})
    assert ul.since_date(cfg, "2026-10-21") == "2026-10-20"   # a newer model wins
    cfg2 = cfg.model_copy(update={"unguided_limit_since": "2026-09-01"})
    assert ul.since_date(cfg2, "2026-10-15") == "2026-09-01"  # the override wins


# ------------------------------------------------------------- surfaces

def test_routes_history_and_watch(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import thesky as rt
    root = _tree(tmp_path, age_s=6 * 3600)
    cfg = _cfg(tmp_path, root)
    monkeypatch.setattr(app, "get_config", lambda: cfg)
    w = asyncio.run(rt.api_thesky_tpoint_watch())
    assert w["text"] == "TPoint: no model seen yet" and w["mode"] == "alert"
    tw.step(cfg, _now())
    h = asyncio.run(rt.api_thesky_tpoint_history())
    assert h["count"] == 2 and h["models"][-1]["points"] == 89 and h["epoch"]["since"]
    assert "unguided limits epoch" in h["text"]
    w = asyncio.run(rt.api_thesky_tpoint_watch())
    assert w["status"] == "idle" and "file_sig" not in w
    assert w["text"].startswith("TPoint model ") and "polar 1.87'" in w["text"]
    json.dumps(w)


def test_cli_history(tmp_path, monkeypatch):
    from typer.testing import CliRunner
    from photonscript import cli
    root = _tree(tmp_path, age_s=6 * 3600)
    cfg = _cfg(tmp_path, root)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    r = CliRunner().invoke(cli.app, ["tpoint-stats", "--history"])
    assert r.exit_code == 0 and "none yet" in r.output
    tw.step(cfg, _now())
    r = CliRunner().invoke(cli.app, ["tpoint-stats", "--history"])
    assert r.exit_code == 0
    assert "2 model(s)" in r.output and "MA -160.1\"" in r.output and "MA -75.8\"" in r.output
    assert "unguided limits epoch: since" in r.output and "watcher: TPoint model" in r.output
    r = CliRunner().invoke(cli.app, ["tpoint-stats", "--history", "--json"])
    assert r.exit_code == 0 and len(json.loads(r.output)["models"]) == 2


def test_remote_cli_allows_tpoint_stats_as_read():
    from photonscript.scheduler import remote_cli as rc
    allow = rc.load_allowlist()
    c = allow.commands["tpoint-stats"]
    assert c.cls == "read" and "--history" in c.flags and not c.write_flags
    assert not rc.check_allowlist(allow)
    assert rc.validate(["tpoint-stats", "--history"], allow).cls == "read"


def test_morning_card_carries_the_tpoint_line():
    from photonscript.scheduler import morning_report as mr
    card = {"date": "2026-10-14", "rigs": [], "tpoint": "TPoint: 228-point run finished"}
    assert "TPoint: 228-point run finished" in mr.card_lines(card)
