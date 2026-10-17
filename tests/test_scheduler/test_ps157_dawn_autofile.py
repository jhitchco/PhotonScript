"""PS-157: at dawn the night files itself into the Library.

Replays the shapes of 2026-10-05 and 2026-10-06: Piggy-600 subs logged "?"
(NINA #2 owns no mount), every sub waiting in review. The dawn pass names
the Piggy subs from the RC16 target timeline (test targets keep their test
names, slews and a parked mount stay "?"), approves the QA-passing ones and
builds the Library.
"""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import dawn_autofile as da
from photonscript.scheduler import identify, runs
from photonscript.shared.config import PhotonScriptConfig

M31 = "Andromeda Galaxy"
HEART = "Heart Nebula"
TT_6934 = "Tracking test NGC 6934"
TT_HPER = "Tracking test h Persei"
FOCUS_7789 = "Focus calibration NGC 7789"


def _cfg(tmp_path, **kw):
    base = dict(_env_file=None, data_dir=str(tmp_path / "data"),
                image_watch_dir=str(tmp_path / "fits"),
                library_dir=str(tmp_path / "lib"),
                nina_logs_dir=str(tmp_path / "logs"),
                stamp_fits_object=False, observatory_tz="UTC",
                piggyback_enabled=True)
    base.update(kw)
    (tmp_path / "data").mkdir(exist_ok=True)
    return PhotonScriptConfig(**base)


@pytest.fixture(autouse=True)
def _quiet(monkeypatch):
    monkeypatch.setattr(runs, "sync_goal_progress", lambda cfg: [])
    monkeypatch.setattr(identify, "_candidates", lambda cfg: [
        (M31, 10.68, 41.27, True), (HEART, 38.2, 61.45, True)])
    monkeypatch.setattr(identify, "_astap_solve", _no_solve)
    da._RUNNING.clear()
    yield
    da._RUNNING.clear()


SOLVES: list = []


def _no_solve(cfg, path):
    SOLVES.append(path)
    return None


def _t(night, hhmm, sec=0):
    return datetime.fromisoformat(f"{night}T{hhmm}:00") + timedelta(seconds=sec)


def _file(cfg, night, rel):
    p = Path(cfg.image_watch_dir) / night / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"SIMPLE  =                    T")
    return str(p)


def _rc16(cfg, night, target, start, end, exp=300, filt="L"):
    """RC16 subs (backfill style: time = DATE-OBS start, no Z)."""
    out, t, i = [], start, 0
    while t + timedelta(seconds=exp) <= end:
        rel = f"LIGHT/rc16_{target[:4]}_{t:%H%M%S}.fits"
        out.append({"rig": "rc16", "file": rel, "abs_path": _file(cfg, night, rel),
                    "time": t.isoformat(), "target": target, "filter": filt,
                    "exp_s": exp, "passed_qa": True, "reason": ""})
        t += timedelta(seconds=exp + 10)
        i += 1
    return out


def _piggy(cfg, night, start, end, exp=120):
    """Piggy-600 subs graded live: time = processing time (end, Z), "?"."""
    out, t = [], start
    while t + timedelta(seconds=exp) <= end:
        en = t + timedelta(seconds=exp)
        rel = f"LIGHT/osc_{t:%H%M%S}.fits"
        out.append({"rig": "piggyback", "file": rel,
                    "abs_path": _file(cfg, night, rel),
                    "time": en.isoformat() + "Z", "target": "?",
                    "filter": "OSC", "exp_s": exp, "passed_qa": True,
                    "reason": ""})
        t = en + timedelta(seconds=8)
    return out


def _mount_log(cfg, night, events):
    """events: [(datetime, state, ra_h, dec)] state tracking|slewing|parked.
    Written like MountLogger: a line on change, a 60 s heartbeat while
    tracking (a parked mount writes one line)."""
    from photonscript.shared.mount_log import log_path
    p = log_path(cfg, night)
    p.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    for (t, st, ra, dec), nxt in zip(events, events[1:] + [None]):
        stop = nxt[0] if nxt else t + timedelta(seconds=1)
        tt, first = t, True
        while tt < stop:
            lines.append({"t": tt.isoformat() + "Z", "rig": "rc16",
                          "ra": ra * 15, "dec": dec, "slewing": st == "slewing",
                          "tracking": st == "tracking", "parked": st == "parked",
                          "pier": "East",
                          "why": "start" if first else "heartbeat"})
            first = False
            if st != "tracking":
                break
            tt += timedelta(seconds=60)
    p.write_text("\n".join(json.dumps(x) for x in lines) + "\n", encoding="utf-8")


def _seed(cfg, night, recs):
    for r in recs:
        runs.append_sub_record(cfg, night, r)


def _subs(cfg, night):
    return runs._load_subs(cfg, night)


def _piggy_names(cfg, night):
    return [(s["file"], s["target"]) for s in _subs(cfg, night)
            if s["rig"] == "piggyback"]


# ------------------------------------------------------------------ 2026-10-05

N5 = "2026-10-05"


def _night_1005(cfg, mount_log=True):
    n = N5
    _seed(cfg, n, _rc16(cfg, n, TT_6934, _t(n, "03:53"), _t(n, "04:48"), exp=60))
    _seed(cfg, n, _rc16(cfg, n, HEART, _t(n, "05:14"), _t(n, "06:38")))
    _seed(cfg, n, _rc16(cfg, n, TT_HPER, _t(n, "07:05"), _t(n, "07:52"), exp=60))
    _seed(cfg, n, _rc16(cfg, n, M31, _t(n, "08:02"), _t(n, "10:14")))
    _seed(cfg, n, _piggy(cfg, n, _t(n, "03:52"), _t(n, "11:28")))
    if mount_log:
        _mount_log(cfg, n, [
            (_t(n, "03:45"), "tracking", 16.5, 7.8),
            (_t(n, "04:50"), "slewing", 16.5, 7.8),
            (_t(n, "04:53"), "tracking", 2.55, 61.4),     # center, AF ...
            (_t(n, "06:40"), "slewing", 2.55, 61.4),
            (_t(n, "06:43"), "tracking", 2.33, 57.1),
            (_t(n, "07:54"), "slewing", 2.33, 57.1),
            (_t(n, "07:57"), "tracking", 0.71, 41.3),
            (_t(n, "11:30"), "parked", 0.0, 90.0),        # mount on M31 to 11:28
        ])


def _name_at(cfg, night, hhmm):
    """The Piggy sub that starts first at or after hhmm."""
    want = _t(night, hhmm)
    for s in sorted(_subs(cfg, night), key=lambda s: s["time"]):
        if s["rig"] != "piggyback":
            continue
        st = datetime.fromisoformat(s["time"].rstrip("Z")) - timedelta(seconds=s["exp_s"])
        if st >= want:
            return s
    raise AssertionError(hhmm)


def test_replay_1005_timeline_names_each_piggy_sub(tmp_path):
    cfg = _cfg(tmp_path)
    _night_1005(cfg)
    SOLVES.clear()
    res = identify.identify_night(cfg, N5, solve=True)
    assert _name_at(cfg, N5, "04:10")["target"] == TT_6934
    assert _name_at(cfg, N5, "05:30")["target"] == HEART
    assert _name_at(cfg, N5, "07:20")["target"] == TT_HPER
    assert _name_at(cfg, N5, "09:00")["target"] == M31
    # the RC16 stopped at 10:14; the mount stayed on M31 until 11:28
    assert _name_at(cfg, N5, "11:20")["target"] == M31
    s = _name_at(cfg, N5, "06:40")                     # through the slew
    assert s["target"] == "?" and s["timeline"]["state"] == "slewing"
    named = _name_at(cfg, N5, "09:00")
    assert named["target_src"] == "rc16-timeline" and named["target_raw"] == "?"
    assert _name_at(cfg, N5, "04:10").get("test") is True
    # no whole-night cluster solve: the held subs are never solved
    assert SOLVES == []
    assert res["timeline"]["named"] > 150
    methods = {c["method"] for c in res["clusters"]}
    assert "rc16 timeline" in methods and "plate solve" not in methods


def test_replay_1005_without_a_mount_log_stays_close_to_the_rc16(tmp_path):
    cfg = _cfg(tmp_path)
    _night_1005(cfg, mount_log=False)
    identify.identify_night(cfg, N5, solve=False)
    assert _name_at(cfg, N5, "09:00")["target"] == M31
    assert _name_at(cfg, N5, "10:16")["target"] == M31   # within 10 min
    late = _name_at(cfg, N5, "11:00")
    assert late["target"] == "?" and late["timeline"]["state"] == "ambiguous"


def test_replay_1005_dawn_filing(tmp_path):
    cfg = _cfg(tmp_path)
    _night_1005(cfg)
    rec = da.file_night(cfg, N5, push=False)
    assert rec["errors"] == []
    subs = _subs(cfg, N5)
    appr = [s for s in subs if s.get("approved_by") == "dawn"]
    assert appr and all(s["reviewed"] and s["review_source"] == "auto"
                        for s in appr)
    names = {s["target"] for s in appr}
    assert names == {M31, HEART}                      # never a test target
    assert {s["rig"] for s in appr} == {"rc16", "piggyback"}
    lib = Path(cfg.library_dir)
    assert any((lib / M31 / "OSC").iterdir())
    assert any((lib / HEART / "L").iterdir())
    assert not (lib / TT_6934).exists() and not (lib / "_").exists()
    assert rec["line"].startswith("Library: ")
    assert f"{M31} " in rec["line"] and "auto-approved at dawn" in rec["line"]
    # the record is stored and readable for the page and the push
    assert da.load_record(cfg, N5)["line"] == rec["line"]
    assert da.morning_line(cfg, N5) == rec["line"]


# ------------------------------------------------------------------ 2026-10-06

N6 = "2026-10-06"


def _night_1006(cfg):
    n = N6
    _seed(cfg, n, _rc16(cfg, n, FOCUS_7789, _t(n, "03:25"), _t(n, "04:14"), exp=60))
    _seed(cfg, n, _rc16(cfg, n, M31, _t(n, "04:17"), _t(n, "09:24")))
    _seed(cfg, n, _piggy(cfg, n, _t(n, "03:26"), _t(n, "09:24")))
    # 22 Piggy lights on a parked, non-tracking mount after 09:26
    parked = _piggy(cfg, n, _t(n, "09:30"), _t(n, "10:17"))
    assert len(parked) == 22
    _seed(cfg, n, parked)
    _mount_log(cfg, n, [
        (_t(n, "03:20"), "tracking", 23.95, 56.7),
        (_t(n, "04:14"), "slewing", 23.95, 56.7),
        (_t(n, "04:16"), "tracking", 0.71, 41.3),
        (_t(n, "09:26"), "parked", 0.0, 90.0),
    ])
    return parked


def test_replay_1006_parked_lights_never_enter_a_goal(tmp_path):
    cfg = _cfg(tmp_path)
    parked = {p["file"] for p in _night_1006(cfg)}
    rec = da.file_night(cfg, N6, push=False)
    subs = {s["file"]: s for s in _subs(cfg, N6)}
    for f in parked:
        assert subs[f]["target"] == "?"
        assert subs[f]["timeline"]["state"] == "parked"
        assert not subs[f].get("reviewed")
    assert _name_at(cfg, N6, "03:40")["target"] == FOCUS_7789
    assert _name_at(cfg, N6, "05:00")["target"] == M31
    appr = [s for s in subs.values() if s.get("approved_by") == "dawn"]
    assert {s["target"] for s in appr} == {M31}
    assert rec["timeline"]["held"]["parked"] == 22
    assert "22 Piggy subs left '?'" in rec["line"]


# ------------------------------------------------------------------ rules

def _small_night(cfg, n="2026-10-08"):
    recs = _rc16(cfg, n, M31, _t(n, "04:00"), _t(n, "05:00"))
    recs += _piggy(cfg, n, _t(n, "04:00"), _t(n, "05:00"))
    return n, recs


def test_manual_target_and_named_subs_are_never_touched(tmp_path):
    cfg = _cfg(tmp_path)
    n, recs = _small_night(cfg)
    pig = [r for r in recs if r["rig"] == "piggyback"]
    pig[0].update(target="Pacman Nebula", target_src="manual")
    pig[1].update(target="Bubble Nebula")
    _seed(cfg, n, recs)
    identify.identify_night(cfg, n, solve=False)
    by = {s["file"]: s for s in _subs(cfg, n)}
    assert by[pig[0]["file"]]["target"] == "Pacman Nebula"
    assert by[pig[1]["file"]]["target"] == "Bubble Nebula"
    assert by[pig[2]["file"]]["target"] == M31


def test_auto_approve_rules(tmp_path):
    cfg = _cfg(tmp_path)
    n, recs = _small_night(cfg)
    pig = [r for r in recs if r["rig"] == "piggyback"]
    pig[0].update(review_source="manual")                 # sent back to review
    pig[1].update(passed_qa=False, reason="hfr")          # rejected
    pig[2].update(auto_verdict="rejected")
    rc = [r for r in recs if r["rig"] == "rc16"]
    rc[0].update(reviewed=True, review_source="auto")     # already approved
    _seed(cfg, n, recs)
    da.file_night(cfg, n, push=False)
    by = {s["file"]: s for s in _subs(cfg, n)}
    assert not by[pig[0]["file"]].get("reviewed")
    assert not by[pig[1]["file"]].get("reviewed")
    assert not by[pig[2]["file"]].get("reviewed")
    assert "approved_by" not in by[rc[0]["file"]]
    assert by[pig[3]["file"]]["approved_by"] == "dawn"


def test_switch_off_files_without_approving(tmp_path):
    cfg = _cfg(tmp_path, auto_approve_at_dawn=False)
    n, recs = _small_night(cfg)
    _seed(cfg, n, recs)
    rec = da.file_night(cfg, n, push=False)
    assert rec["approved"]["approved"] == 0
    assert not any(s.get("reviewed") for s in _subs(cfg, n))
    assert _name_at(cfg, n, "04:10")["target"] == M31     # still attributed
    assert rec["line"] == "Library: 0 subs filed for sync"


def test_ambiguous_subs_fall_back_to_the_ps137_solve(tmp_path, monkeypatch):
    from photonscript.scheduler import piggy_attribution as pa
    cfg = _cfg(tmp_path)
    n = "2026-10-09"
    recs = _piggy(cfg, n, _t(n, "04:00"), _t(n, "04:30"))   # no RC16 at all
    _seed(cfg, n, recs)
    seen = {}

    def fake(config, date, subs, apply, solve=False, max_solves=30, **kw):
        if solve:              # the dawn fallback (not the report pass)
            seen.update(n=len(subs), apply=apply, solve=solve,
                        states={s["timeline"]["state"] for s in subs})
        return {"changed": [], "piggy": 0}
    monkeypatch.setattr(pa, "attribute_records", fake)
    rec = da.file_night(cfg, n, push=False)
    assert seen == {"n": len(recs), "apply": True, "solve": True,
                    "states": {"ambiguous"}}
    assert rec["solve_fallback"]["tried"] == len(recs)


def test_correlation_never_names_a_held_sub(tmp_path):
    cfg = _cfg(tmp_path)
    _night_1006(cfg)
    runs.attribute_night(cfg, N6)
    held = [s for s in _subs(cfg, N6)
            if (s.get("timeline") or {}).get("state") == "parked"]
    assert len(held) == 22 and all(s["target"] == "?" for s in held)


def test_summary_line():
    assert da.summary_line({}) == "Library: 0 subs filed for sync"
    line = da.summary_line({M31: 180, HEART: 32}, approved=150, held=3)
    assert line == ("Library: 212 subs filed for sync (Andromeda Galaxy 180, "
                    "Heart Nebula 32); 150 auto-approved at dawn; 3 Piggy subs "
                    "left '?'")


def test_post_night_warm_starts_the_dawn_filing(tmp_path, monkeypatch):
    from photonscript.shared import phd2_store
    cfg = _cfg(tmp_path)
    n, recs = _small_night(cfg)
    _seed(cfg, n, recs)
    started = []
    monkeypatch.setattr(phd2_store, "night_of", lambda c, w=None: n)
    monkeypatch.setattr(da, "start_dawn_filing",
                        lambda c, d: started.append(d) or True)
    monkeypatch.setattr(runs, "start_thumb_warm", lambda *a, **k: None)
    monkeypatch.setattr(runs, "start_backfill", lambda *a, **k: None)
    runs.post_night_warm(cfg)
    assert started == [n]


def test_dawn_filing_waits_for_grading_bounded(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    t = {"now": 0.0}
    monkeypatch.setattr(runs, "backfill_status",
                        lambda c, d: {"running": True, "pending": 0})

    def sleep(s):
        t["now"] += s
    assert da._wait_backfill(cfg, "2026-10-08", sleep=sleep,
                             clock=lambda: t["now"]) is False
    assert t["now"] >= da.WAIT_BACKFILL_MAX_S
    monkeypatch.setattr(runs, "backfill_status",
                        lambda c, d: {"running": False, "pending": 0})
    assert da._wait_backfill(cfg, "2026-10-08", sleep=sleep) is True


def test_config_default_and_system_field():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    assert PhotonScriptConfig(_env_file=None).auto_approve_at_dawn is True
    f = {x[0]: x for x in _CONFIG_FIELDS}["auto_approve_at_dawn"]
    assert f[1] == "PS_AUTO_APPROVE_AT_DAWN" and f[4] == "bool"


def test_endpoints(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import autofile as r
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    n, recs = _small_night(cfg)
    _seed(cfg, n, recs)
    assert r.api_autofile_record(n).status_code == 404
    out = r.api_autofile_run(n, {"approve": False})
    assert out["approved"]["approved"] == 0
    assert r.api_autofile_record(n)["date"] == n
    paths = {getattr(x, "path", "") for x in r.router.routes}
    assert "/api/runs/{date}/autofile" in paths
    assert app._autofile_router is r
