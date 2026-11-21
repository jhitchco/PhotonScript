"""PS-155 (PHD2 guiding that sends no corrections: morning analysis, audit
row, the agent's page) and PS-156 (the unguided fallback: alert / auto, the
per-filter cap, the watchdog trigger, the guard route)."""
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import guide_fallback as gf
from photonscript.scheduler import phd2_analysis as pa
from photonscript.scheduler import phd2_audit as audit
from photonscript.scheduler import phd2_logs as pl
from photonscript.scheduler.armer import Armer
from photonscript.shared import phd2_store as store
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
from photonscript.shared.night_events import events_path

FIX = Path(__file__).parent / "fixtures" / "phd2"
N06 = "PHD2_GuideLog_2026-10-06_203731.txt"
N25 = "PHD2_GuideLog_2026-09-25_192414.txt"


def _cfg(tmp_path=None, **kw):
    if tmp_path is not None:
        kw.setdefault("data_dir", tmp_path / "data")
    kw.setdefault("guide_fallback_test_date", "")     # config lengths, no report
    return PhotonScriptConfig(_env_file=None, **kw)


def _session(name, start, cfg=None):
    secs = pl.parse_guide_log((FIX / name).read_text(encoding="utf-8"), name)
    a = pa.analyze_sections(secs, cfg or _cfg())
    return next(s for s in a["sessions"] if s["start_local"].endswith(start))


# ---- PS-155 morning analysis -----------------------------------------------------

def test_analysis_flags_the_1006_no_pulse_session():
    s = _session(N06, "22:20:54")
    nc = s["no_corrections"]
    assert nc["runs"] == 1 and nc["longest"] > 250 and nc["threshold_px"] == 5.0
    ids = {f["id"] for f in s["findings"]}
    assert "no_corrections" in ids and "guide_output_off" not in ids
    f = next(f for f in s["findings"] if f["id"] == "no_corrections")
    assert f["severity"] == "critical" and "pulseguide command failed" in f["recommendation"]
    assert s["settings"]["guide_output"] is True        # the header said enabled


def test_analysis_leaves_a_guiding_assistant_run_alone():
    s = _session(N25, "20:53:31")
    ids = {f["id"] for f in s["findings"]}
    assert "no_corrections" not in ids and "guide_output_off" not in ids
    assert s["no_corrections"]["output_off_frames"] > 20 and s["no_corrections"]["ga"]


# ---- PS-155 audit row ------------------------------------------------------------

def _eval(observed):
    cfg = _cfg()
    obs = {s: {} for s in audit.SOURCES}
    obs.update(observed)
    res = audit.evaluate(audit.load_desired(cfg), obs, cfg)
    return {r["id"]: r for r in res["rows"]}


def test_guide_output_row_fails_when_off_and_reads_the_log():
    d = audit.load_desired(_cfg())
    assert not d.get("lint"), d.get("lint")
    rows = _eval({"api": {"guide_output": False}})
    r = rows["guide_output"]
    assert r["status"] == audit.FAIL and r["apply"] == "manual"
    assert "Shared Parameters" in r["fix"] and "Enable mount guide output" in r["fix"]
    assert _eval({"api": {"guide_output": True}})["guide_output"]["status"] == audit.PASS
    assert _eval({"log": {"guide_output": True}})["guide_output"]["status"] == audit.PASS
    assert _eval({})["guide_output"]["status"] == audit.UNKNOWN


def test_log_observed_carries_guide_output():
    secs = pl.parse_guide_log((FIX / N06).read_text(encoding="utf-8"), N06)
    assert audit.log_observed(secs, _cfg())["guide_output"] is True


# ---- PS-156 the fallback module --------------------------------------------------

def _target(name, guided, *plans):
    return NinaSequenceTarget(
        name=name, ra_hours=1.0, dec_degrees=20.0, start_guiding=guided,
        exposures=[ExposurePlan(filter_type=f, exposure_seconds=s, count=c)
                   for f, s, c in plans])


def test_modes_lengths_and_the_per_filter_cap():
    assert gf.mode(_cfg()) == "alert"
    assert gf.mode(_cfg(guide_fallback_mode="AUTO")) == "auto"
    assert gf.mode(_cfg(guide_fallback_mode="bogus")) == "alert"
    cfg = _cfg()
    lens = gf.lengths(cfg, ["L", "Ha"])
    assert lens["L"][0] == 60 and lens["Ha"][0] == 300
    assert "L 60 s" in gf.plan_text(lens)
    ts = [_target("M31", False, (FilterType.LUMINANCE, 180, 10), (FilterType.HA, 600, 4)),
          _target("Guided", True, (FilterType.LUMINANCE, 180, 10))]
    notes = gf.cap_targets(cfg, ts)
    e = {x.filter_type.value: x for x in ts[0].exposures}
    assert (e["L"].exposure_seconds, e["L"].count) == (60, 30)      # same integration
    assert (e["Ha"].exposure_seconds, e["Ha"].count) == (300, 8)
    assert ts[1].exposures[0].exposure_seconds == 180                # guided: untouched
    assert len(notes) == 2


# ---- PS-156 the armer ------------------------------------------------------------

async def _noop(*a, **k):
    return None


def _armer(tmp_path, monkeypatch, notes, **kw):
    a = Armer(_cfg(tmp_path, **kw))
    a.state, a.plan = "RUNNING", {"night_of": "2026-10-06"}
    a.guiding_override = "guided"
    import photonscript.scheduler.armer as armer_mod

    async def _notify(cfg, msg, **k):
        notes.append((msg, k.get("priority")))
    monkeypatch.setattr(armer_mod, "notify", _notify)
    return a


def _events(cfg, night="2026-10-06"):
    return [r for r in store.read_jsonl(events_path(cfg, night))
            if r.get("kind") == "guide_fallback"]


async def test_alert_mode_records_and_pushes_once_and_switches_nothing(tmp_path, monkeypatch):
    notes = []
    a = _armer(tmp_path, monkeypatch, notes)
    called = []

    async def _fb(reason):
        called.append(reason)
        return True
    monkeypatch.setattr(a, "fallback_unguided", _fb)
    rec = await a.guide_fallback("guard D7: not correcting", source="guard D7")
    assert rec["mode"] == "alert" and rec["acted"] is False and called == []
    assert len(notes) == 1 and "would run the rest of the night unguided" in notes[0][0]
    assert "L 60 s" in notes[0][0]
    assert await a.guide_fallback("again") is None                 # once per night
    assert len(notes) == 1
    ev = _events(a.config)
    assert len(ev) == 1 and ev[0]["value"] == "alert"
    saved = json.loads((tmp_path / "data" / "armer_state.json").read_text())
    assert saved["guide_fallback"]["night"] == "2026-10-06"


async def test_auto_mode_runs_the_fallback_and_caps_the_redispatch(tmp_path, monkeypatch):
    notes = []
    a = _armer(tmp_path, monkeypatch, notes, guide_fallback_mode="auto")
    calls = []

    async def _nina(key, *x, **k):
        calls.append(key)
        return {"Success": True}

    async def _dispatch(companion=True, fail_state="ERROR"):
        calls.append(("dispatch", companion))
        return True
    monkeypatch.setattr(a, "_nina", _nina)
    monkeypatch.setattr(a, "_dispatch_and_start", _dispatch)
    rec = await a.guide_fallback("guard D7", source="guard D7")
    assert rec["acted"] is True and rec["ok"] is True
    assert calls == ["sequence_stop", "guider_stop", ("dispatch", False)]
    assert a.guiding_override == "unguided" and a._fallback_night == "2026-10-06"
    assert len(notes) == 1 and notes[0][1] == 1          # fallback_unguided's page
    assert "subs capped" in notes[0][0]
    assert _events(a.config)[0]["value"] == "unguided"


async def test_skips_off_unguided_not_running_and_watching(tmp_path, monkeypatch):
    notes = []
    a = _armer(tmp_path, monkeypatch, notes, guide_fallback_mode="off")
    assert await a.guide_fallback("x") is None
    b = _armer(tmp_path / "b", monkeypatch, notes)
    b.guiding_override = "unguided"
    assert await b.guide_fallback("x") is None
    c = _armer(tmp_path / "c", monkeypatch, notes)
    c.state = "ARMED"
    assert await c.guide_fallback("x") is None
    assert notes == []
    d = _armer(tmp_path / "d", monkeypatch, notes, guide_fallback_mode="auto")
    d.state = "WATCHING"
    rec = await d.guide_fallback("x")
    assert rec["acted"] is False and "sideloaded" in rec["note"] and len(notes) == 1


async def test_watchdog_hands_a_long_loss_to_the_fallback(tmp_path, monkeypatch):
    notes = []
    a = _armer(tmp_path, monkeypatch, notes, guiding_auto_recover=False)
    a.plan["dusk_utc"] = "2026-10-07T02:00:00Z"

    async def _nina(key, **kw):
        return {"Response": {"Connected": True, "State": "Stopped"}}
    a._nina = _nina
    seen = []

    async def _gfb(reason, source=""):
        seen.append((reason, source))
    monkeypatch.setattr(a, "guide_fallback", _gfb)
    t0 = datetime(2026, 10, 7, 3, 0, 0)
    await a._maybe_warn_not_guiding(t0)
    await a._maybe_warn_not_guiding(t0 + timedelta(minutes=9))
    assert seen == []
    await a._maybe_warn_not_guiding(t0 + timedelta(minutes=10))
    assert len(seen) == 1 and seen[0][1] == "watchdog" and "10 min" in seen[0][0]


async def test_request_guide_fallback_without_an_armer(monkeypatch):
    import sys
    from photonscript.scheduler.armer import request_guide_fallback
    monkeypatch.delitem(sys.modules, "photonscript.scheduler.app", raising=False)
    assert await request_guide_fallback(_cfg(), "x") is None


# ---- PS-155 the agent's page -----------------------------------------------------

async def test_agent_pages_once_per_night_and_asks_for_the_fallback(tmp_path, monkeypatch):
    from photonscript.telescope_agent import agent as agent_mod
    from photonscript.telescope_agent.guide_guard import NO_CORR, Verdict
    import photonscript.scheduler.armer as armer_mod
    cfg = _cfg(tmp_path)
    pages, asked = [], []

    async def _notify(c, msg, **k):
        pages.append((msg, k.get("priority")))

    async def _req(c, reason, source=""):
        asked.append(source)
    monkeypatch.setattr(agent_mod, "notify", _notify)
    monkeypatch.setattr(armer_mod, "request_guide_fallback", _req)

    class _State:
        current_target = "M31"

    ag = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    ag.config, ag.state = cfg, _State()
    v = Verdict("D7", NO_CORR, "PHD2 is guiding but sending no corrections: 20 frames",
                {"frames": 20})
    now = datetime(2026, 10, 7, 4, 0, 0)
    await ag._nocorr_alarm([v], "2026-10-06", now)
    await ag._nocorr_alarm([v], "2026-10-06", now)
    assert len(pages) == 1 and pages[0][1] == 1
    msg = pages[0][0]
    assert "PulseGuide" in msg and "Shared Parameters" in msg and "Max RA / Dec" in msg
    assert asked == ["guard D7", "guard D7"]
    ev = [r for r in store.read_jsonl(events_path(cfg, "2026-10-06"))
          if r.get("kind") == "no_corrections"]
    assert len(ev) == 2 and ev[0]["value"] == "D7"


# ---- routes and config -------------------------------------------------------------

def test_guard_route_counts_no_correction_episodes_and_the_fallback(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import phd2 as r
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app, "_config", cfg)
    store.append_jsonl(store.guard_path(cfg, "2026-10-06"), {
        "event": "open", "id": "e1", "t_utc": "2026-10-07T04:00:00Z",
        "kind": "no_corrections", "codes": ["D7"]})
    store.append_jsonl(events_path(cfg, "2026-10-06"), {
        "t": "2026-10-07T04:01:00Z", "kind": "guide_fallback", "value": "alert"})
    g = r.api_phd2_guard(date="2026-10-06")
    assert g["no_corrections"] == 1 and g["non_star"] == 0
    assert g["fallback"]["value"] == "alert"
    # a no-correction episode never marks subs as a non-star lock
    assert store.nonstar_windows(cfg, "2026-10-06") == []
    by_env = {f[1]: f for f in app._CONFIG_FIELDS}
    for env in ("PS_PHD2_NOCORR_FRAMES", "PS_PHD2_NOCORR_PX", "PS_PHD2_DRIFT_WINDOW_MIN",
                "PS_GUIDE_FALLBACK_MODE", "PS_GUIDE_FALLBACK_AFTER_MIN"):
        assert hasattr(cfg, by_env[env][0])


def test_new_sources_are_ascii():
    root = Path(__file__).resolve().parents[2]
    for rel in ("photonscript/scheduler/guide_fallback.py",
                "photonscript/telescope_agent/guide_guard.py",
                "photonscript/shared/guide_motion.py",
                "config/phd2/desired_oag_rc16.toml"):
        assert all(b < 128 for b in (root / rel).read_bytes()), rel
