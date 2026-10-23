"""Dawn filing fix (2026-10-11): every armed night files once.

Replays night 2026-10-09: armed with the TPoint recipe, the RC16 took nine
TPoint points (test subs), parked at 03:07:24Z, then NINA #1 lost the mount
(the 03:08:03Z poll reads RA 0, Dec 0, no alt / az) while the Piggy-600 kept
taking 450 s lights to 06:45Z. The night ended DISARMED, so the armer's dawn
shutdown never filed it, and the mount log's disconnected line held every
later Piggy sub as "parked" (never solved, never approved).
"""
import json
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import dawn_autofile as da
from photonscript.scheduler import identify, runs
from photonscript.shared.mount_log import log_path

from tests.test_scheduler.test_ps157_dawn_autofile import (  # noqa: F401
    M31, HEART, _cfg, _piggy, _quiet, _rc16, _seed, _subs, _t)

N9 = "2026-10-09"


def _write_lines(cfg, night, lines):
    p = log_path(cfg, night)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n",
                 encoding="utf-8")


def _line(t, ra, dec, tracking=False, parked=False, slewing=False,
          alt=40.0, az=120.0, why="move"):
    return {"t": t.isoformat() + "Z", "rig": "rc16", "ra": ra, "dec": dec,
            "alt": alt, "az": az, "pier": "West", "slewing": slewing,
            "tracking": tracking, "parked": parked, "why": why}


def _plan(cfg, night, dawn, windows=()):
    snap = {"night_of": night, "dawn_utc": dawn.isoformat() + "Z",
            "targets": [{"name": n, "exposures": [],
                         "window_start_utc": a.isoformat() + "Z",
                         "window_end_utc": b.isoformat() + "Z"}
                        for n, a, b in windows]}
    p = runs.runs_dir(cfg) / f"{night}_plan.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(snap), encoding="utf-8")


def _night_1009(cfg, windows=True):
    n = N9
    tp = []
    for i, hhmm in enumerate(("02:57", "02:58", "03:00", "03:02", "03:05")):
        tp += _rc16(cfg, n, f"TPoint point {i + 1:02d}/45 alt 38 az 72 (east)",
                    _t(n, hhmm), _t(n, hhmm, 5), exp=5)
    _seed(cfg, n, tp)
    late = _piggy(cfg, n, _t(n, "03:13"), _t(n, "06:46"), exp=450)
    _seed(cfg, n, late)
    lines = [_line(_t(n, "02:56"), 5.0, 42.0, tracking=True, why="start")]
    lines += [_line(_t(n, "03:06", 58), 5.0, 42.0, tracking=True,
                    slewing=True, why="slew-start"),
              _line(_t(n, "03:07", 24), 52.9, 89.5, parked=True, alt=31.8,
                    az=0.5, why="slew-end"),
              # NINA #1 lost the mount: the disconnected poll
              _line(_t(n, "03:08", 3), 0.0, 0.0, alt=None, az=None,
                    why="unpark")]
    _write_lines(cfg, n, lines)
    _plan(cfg, n, _t(n, "11:56"), [
        (M31, _t(n, "02:09"), _t(n, "05:13")),
        (HEART, _t(n, "05:13"), _t(n, "11:56"))] if windows else [])
    return late


def test_root_cause_disconnected_poll_held_subs_as_parked(tmp_path):
    """Before the fix the 03:08 line made every later sub 'parked' (it
    read as an idle mount). mount_log now reads that legacy 0 / 0 line as
    an offline marker too, so its segment is "offline", never "idle"."""
    from photonscript.shared import mount_log
    cfg = _cfg(tmp_path)
    late = _night_1009(cfg)
    lines = mount_log.load(cfg, N9)
    old = mount_log.segments(lines, end=_t(N9, "06:46"))
    assert old[-1]["state"] == "offline" and old[-1]["end"] == _t(N9, "06:46")
    s = dict(late[5])
    assert da.judge_sub(s, cfg, [], old)["state"] != "parked"
    # what the pre-fix reader saw: the 0 / 0 line as an idle mount
    idle = [{**g, "state": "idle"} if g["state"] == "offline" else g
            for g in old]
    assert da.judge_sub(s, cfg, [], idle)["state"] == "parked"
    # the fix: the disconnected poll ends the state; after it, unknown
    new = da._mount_states(lines, _t(N9, "06:46"))
    assert new[-1]["state"] == "parked" and new[-1]["end"] == _t(N9, "03:08", 3)
    assert da.judge_sub(s, cfg, [], new)["state"] == "ambiguous"
    assert da.mount_offline(lines[-1]) and not da.mount_offline(lines[0])


def test_replay_1009_files_solves_and_never_guesses(tmp_path, monkeypatch):
    from photonscript.scheduler import piggy_attribution as pa
    cfg = _cfg(tmp_path)
    late = _night_1009(cfg)
    solved = {}

    def fake(config, date, subs, apply, solve=False, max_solves=30, **kw):
        if not solve:
            return {"changed": [], "piggy": 0}
        solved["n"] = len(subs)
        # the frames solve onto the Heart Nebula for the first three only
        for s in subs[:3]:
            s.update(target=HEART, target_src="piggy-frame")
        return {"changed": subs[:3], "piggy": len(subs)}
    monkeypatch.setattr(pa, "attribute_records", fake)
    rec = da.file_night(cfg, N9, push=False)
    assert rec["errors"] == []
    by = {s["file"]: s for s in _subs(cfg, N9)}
    # the first sub sits within 10 min of the last TPoint point: the PS-51
    # correlation lends it that test name (never approved); every later sub
    # is tried by the solve
    first = by[late[0]["file"]]
    from photonscript.shared.qa_rules import is_test_record
    assert first["target"].startswith("TPoint") and is_test_record(first)
    assert not first.get("reviewed")
    assert solved["n"] == len(late) - 1
    late = late[1:]
    appr = [s for s in by.values() if s.get("approved_by") == "dawn"]
    assert len(appr) == 3 and {s["target"] for s in appr} == {HEART}
    # the plan windows exist but the mount was never seen tracking: no guess
    assert rec["plan_fallback"]["named"] == 0
    assert rec["plan_fallback"]["skipped"].get("mount not seen tracking")
    rest = [by[s["file"]] for s in late[3:]]
    assert all(s["target"] == "?" and not s.get("reviewed") for s in rest)
    assert not any(s.get("reviewed") for s in by.values() if s["rig"] == "rc16")
    assert da.load_record(cfg, N9)["line"].startswith("Library: 3 subs")


def test_plan_window_names_a_sub_only_with_mount_evidence(tmp_path):
    cfg = _cfg(tmp_path)
    n = "2026-10-12"
    pig = _piggy(cfg, n, _t(n, "04:00"), _t(n, "05:00"), exp=300)
    _seed(cfg, n, pig)
    # tracking on M31 (RA 0.71 h) until 04:30, then on a field far away
    lines = []
    t = _t(n, "03:59")
    while t < _t(n, "05:01"):
        far = t >= _t(n, "04:30")
        lines.append(_line(t, 100.0 if far else 10.68, 20.0 if far else 41.27,
                           tracking=True, why="heartbeat"))
        t += timedelta(seconds=60)
    _write_lines(cfg, n, lines)
    _plan(cfg, n, _t(n, "11:56"), [(M31, _t(n, "03:00"), _t(n, "06:00"))])
    subs = _subs(cfg, n)
    out = da.plan_fallback_records(cfg, n, subs)
    early = [s for s in subs if s["rig"] == "piggyback"
             and s["time"] < _t(n, "04:28").isoformat()]
    assert early and all(s["target"] == M31 and s["target_src"] == "plan-window"
                         and s["target_raw"] == "?" for s in early)
    assert out["skipped"].get("mount elsewhere")
    assert all(s["target"] == "?" for s in subs
               if s["rig"] == "piggyback" and s not in early
               and s["time"] > _t(n, "04:36").isoformat())
    # a manual target is never touched; no windows, no names
    manual = dict(pig[0], target="?", target_src="manual")
    assert da.plan_fallback_records(cfg, n, [manual])["named"] == 0
    assert da.plan_fallback_records(cfg, "2026-10-13", [dict(pig[0])]) \
        ["named"] == 0


# ------------------------------------------------------------------ sweep

def test_sweep_files_an_unfiled_armed_night_once(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _night_1009(cfg)
    started = []

    def start(c, d):
        started.append(d)
        return True
    after = _t(N9, "11:56") + timedelta(minutes=da.SWEEP_AFTER_DAWN_MIN + 1)
    # too early: the armer's own shutdown runs first
    assert da.sweep(cfg, "DISARMED", None, now=_t(N9, "12:30"),
                    start=start) == []
    # the armer still owns the night: its dawn shutdown files it
    assert da.sweep(cfg, "RUNNING", N9, now=after, start=start) == []
    assert da.sweep(cfg, "PAUSED_UNSAFE", N9, now=after, start=start) == []
    # disarmed (2026-10-09), error, complete, or armed for the next night
    for state, night in (("DISARMED", None), ("ERROR", N9),
                         ("ARMED", "2026-10-10"), ("COMPLETE", N9)):
        assert da.sweep(cfg, state, night, now=after, start=start) == [N9]
    # once filed (a record), never again
    da.file_night(cfg, N9, push=False)
    assert da.sweep(cfg, "DISARMED", None, now=after, start=start) == []
    # a running filing is not started twice
    da.record_path(cfg, N9).unlink()
    da._RUNNING[N9] = True
    assert da.due_nights(cfg, after, "DISARMED") == []


def test_sweep_window_and_switch(tmp_path):
    cfg = _cfg(tmp_path)
    _night_1009(cfg)
    later = datetime(2026, 10, 14, 18, 0)
    assert da.due_nights(cfg, later) == []          # older than 3 days
    cfg3 = _cfg(tmp_path, dawn_filing_catchup_days=6)
    assert da.due_nights(cfg3, later) == [N9]
    off = _cfg(tmp_path, dawn_filing_catchup_days=0)
    assert da.due_nights(off, _t(N9, "14:00")) == []
    # a plan without subs (nothing captured) is not filed
    _plan(cfg, "2026-10-08", _t("2026-10-08", "11:56"))
    assert da.due_nights(cfg, _t(N9, "14:00")) == [N9]


def test_run_sweep_loop_reads_the_armer(tmp_path, monkeypatch):
    import asyncio
    cfg = _cfg(tmp_path)
    seen = []
    monkeypatch.setattr(da, "sweep", lambda c, st, night: seen.append((st, night)))

    class Armer:
        state = "ARMED"
        plan = {"night_of": "2026-10-11"}

    async def go():
        task = asyncio.create_task(da.run_sweep(lambda: cfg, lambda: Armer(),
                                                tick_seconds=0))
        await asyncio.sleep(0.01)
        task.cancel()
    asyncio.run(go())
    assert seen and seen[0] == ("ARMED", "2026-10-11")


def test_preview_writes_nothing(tmp_path):
    cfg = _cfg(tmp_path)
    late = _night_1009(cfg)
    before = (runs.runs_dir(cfg) / f"{N9}_subs.jsonl").read_bytes()
    r = da.preview_night(cfg, N9)
    assert r["dry_run"] and not r["record"]
    # (the dry run skips the PS-51 correlation: the first sub counts too)
    assert r["would_solve"] == len(late) and r["would_approve"] == 0
    assert r["piggy_still_unknown"] == len(late)
    assert (runs.runs_dir(cfg) / f"{N9}_subs.jsonl").read_bytes() == before
    assert da.load_record(cfg, N9) is None


def test_cli_dawn_file(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    cfg = _cfg(tmp_path)
    _night_1009(cfg)
    monkeypatch.setattr(cli, "_config_for_repo", lambda repo: cfg)
    res = CliRunner().invoke(cli.app, ["dawn-file", "--night", N9,
                                       "--dry-run", "--json"])
    assert res.exit_code == 0, res.output
    assert json.loads(res.output)["dry_run"] is True
    assert da.load_record(cfg, N9) is None
    res = CliRunner().invoke(cli.app, ["dawn-file", "--night", N9,
                                       "--no-approve"])
    assert res.exit_code == 0, res.output
    assert "Library: 0 subs filed" in res.output
    assert da.load_record(cfg, N9)["auto_approve"] is False


def test_plan_snapshot_keeps_the_windows(tmp_path):
    from types import SimpleNamespace
    cfg = _cfg(tmp_path)
    t = SimpleNamespace(name=M31, exposures=[],
                        window_start_utc=datetime(2026, 10, 11, 2, 9, 56, 9),
                        window_end_utc=None)
    runs.save_plan_snapshot(cfg, "2026-10-10", {"dawn_utc": "x"}, [t])
    snap = da.plan_snapshot(cfg, "2026-10-10")
    assert snap["targets"][0]["window_start_utc"] == "2026-10-11T02:09:56Z"
    assert snap["targets"][0]["window_end_utc"] is None
    assert da.plan_windows(snap) == []


def test_config_default_and_system_field():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    from photonscript.shared.config import PhotonScriptConfig
    assert PhotonScriptConfig(_env_file=None).dawn_filing_catchup_days == 3
    f = {x[0]: x for x in _CONFIG_FIELDS}["dawn_filing_catchup_days"]
    assert f[1] == "PS_DAWN_FILING_CATCHUP_DAYS" and f[4] == "int"


def test_startup_spawns_the_sweep():
    import inspect

    from photonscript.scheduler import app
    assert "run_sweep" in inspect.getsource(app._start_dawn_filing_sweep)


@pytest.mark.parametrize("line,offline", [
    ({"ra": 0.0, "dec": 0.0, "alt": None, "az": None}, True),
    ({"ra": 0.0, "dec": 0.0, "alt": 12.0, "az": 3.0}, False),
    ({"ra": 10.0, "dec": 0.0, "alt": None, "az": None}, False),
    ({"ra": None, "dec": None}, False),
])
def test_mount_offline(line, offline):
    assert da.mount_offline(line) is offline
