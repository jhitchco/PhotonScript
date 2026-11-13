"""Quiet carriers (2026-10-25, scheduler/carrier_quiet.py).

On a moon-fill night the RC16 shoots Ha as a pointing carrier for the
Piggy-600; unguided its subs are expected to be rejected. Nothing alarms or
acts on those rejects:

  1. the lookup: armer plan, armer_state.json, the plan snapshot; guided
     carriers and the config key turn it off;
  2. the night pilot: no RC16 action or push on an unguided carrier (shorten,
     AF, a human, the sub ladder); PHD2 on an unguided night still stops
     (shared mount); the Piggy-600 unchanged; a guided carrier is normal;
  3. the sub doctor: no RC16 push (guider_conflict still), focuser info
     unless the Piggy-600 fails too; the morning line skips carrier time;
  4. the telescope agent: no reject streak, RC16 AF stall info only;
  5. the AF report grader; the focuser guard waits instead of judging a
     carrier sub;
  6. the night story and the morning report: "RC16 carrier for the
     Piggy-600, N subs, not judged"; a guided carrier night is unchanged.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from photonscript.scheduler import carrier_quiet as cq
from photonscript.scheduler import morning_report as mr
from photonscript.scheduler import night_pilot as npil
from photonscript.scheduler import night_story as ns
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import append_jsonl, read_jsonl
from tests.test_scheduler.test_night_pilot import (
    NIGHT, FakeArmer, FakeIO, T, _cfg, _diag, _heart, _pilot_events, _sub,
    _tick, _tonight_bad_subs)

ROOT = Path(__file__).resolve().parents[2]
EM = chr(0x2014)   # never in a text (house rule)


def _mf(guided=False, target="Heart Nebula", start="2026-10-13T02:00:00Z",
        end="2026-10-13T12:00:00Z"):
    return {"active": True, "guided": guided, "reason": "100% moon",
            "carriers": [{"target": target, "window_start_utc": start,
                          "window_end_utc": end, "filter": "Ha",
                          "exposure_s": 300.0, "count": 100,
                          "expect_rejected": guided is not True}]}


@pytest.fixture(autouse=True)
def _no_app_armer(monkeypatch):
    """No in-process armer left over from another test."""
    import sys
    app_mod = sys.modules.get("photonscript.scheduler.app")
    if app_mod is not None:
        monkeypatch.setattr(app_mod, "_armer", None, raising=False)


def _carrier_armer(guided=False, **kw):
    a = FakeArmer(guided=guided, **kw)
    a.plan["moon_fill"] = _mf(guided=guided)
    return a


def _save_state(cfg, guided=False, state="RUNNING"):
    p = Path(cfg.data_dir) / "armer_state.json"
    p.write_text(json.dumps({
        "state": state, "guiding_override": "guided" if guided else "unguided",
        "plan": {"night_of": NIGHT, "moon_fill": _mf(guided=guided)}}),
        encoding="utf-8")


# ------------------------------------------------------------- 1. lookup

def test_lookup_by_name_window_guided_and_key(tmp_path):
    cfg = _cfg(tmp_path)
    a = _carrier_armer()
    assert cq.carrier_for(cfg, target="Heart Nebula", armer=a)["target"] == "Heart Nebula"
    assert cq.carrier_for(cfg, target="heart nebula", armer=a) is not None
    assert cq.carrier_for(cfg, target="M31", armer=a) is None
    assert cq.carrier_for(cfg, when=T("04:00"), armer=a) is not None
    assert cq.carrier_for(cfg, when=datetime(2026, 10, 13, 1, 0), armer=a) is None
    # never the Piggy-600
    assert cq.carrier_for(cfg, rig="piggyback", target="Heart Nebula", armer=a) is None
    # a guided carrier (bonus Ha): normal behavior
    assert cq.carrier_for(cfg, target="Heart Nebula", armer=_carrier_armer(True)) is None
    # a guided plan that fell back to unguided: the armer's mode now wins
    g = _carrier_armer(True)
    g.guided = False
    assert cq.carrier_for(cfg, target="Heart Nebula", armer=g) is not None
    # the key turns it off
    off = _cfg(tmp_path, plan_moon_fill_carrier_quiet=False)
    assert cq.carrier_for(off, target="Heart Nebula", armer=a) is None
    assert cq.quiet_targets(off, armer=a) == {}
    assert _cfg(tmp_path).plan_moon_fill_carrier_quiet is True


def test_lookup_from_state_file_and_plan_snapshot(tmp_path):
    from photonscript.scheduler.runs import save_plan_snapshot
    from photonscript.shared.models import NinaSequenceTarget
    cfg = _cfg(tmp_path)
    _save_state(cfg)
    assert cq.carrier_for(cfg, target="Heart Nebula", night=NIGHT) is not None
    assert cq.carrier_for(cfg, target="Heart Nebula", night="2026-10-11") is None
    # the morning after: the armer moved on, the snapshot keeps the carriers
    (Path(cfg.data_dir) / "armer_state.json").unlink()
    t = NinaSequenceTarget(name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                           exposures=[], carrier=True)
    save_plan_snapshot(cfg, NIGHT, {"moon_fill": _mf()}, [t])
    snap = json.loads((Path(cfg.data_dir) / "runs" / f"{NIGHT}_plan.json").read_text())
    assert snap["moon_fill"]["carriers"][0]["target"] == "Heart Nebula"
    assert snap["targets"][0]["carrier"] is True
    assert set(cq.quiet_targets(cfg, night=NIGHT)) == {"heartnebula"}
    save_plan_snapshot(cfg, NIGHT, {"moon_fill": _mf(guided=True)}, [t])
    assert cq.quiet_targets(cfg, night=NIGHT) == {}


def test_config_field_registered():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    by = {f[0]: f for f in _CONFIG_FIELDS}
    f = by["plan_moon_fill_carrier_quiet"]
    assert f[1] == "PS_PLAN_MOON_FILL_CARRIER_QUIET" and f[4] == "bool"
    src = (ROOT / "photonscript/scheduler/carrier_quiet.py").read_text(encoding="utf-8")
    assert EM not in src and EM not in f[2]


# ------------------------------------------------------------- 2. night pilot

def test_pilot_shorten_on_carrier_no_action_no_push(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = _carrier_armer(), FakeIO(cfg, phd2="Stopped")
    proj = _heart(io)
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "tracking", "shorten_subs:180", ecc=0.76, exp_s=300,
          shared_mount=True, trail={"rate_arcsec_min": 0.45})
    out = _tick(cfg, armer, io, "04:30")
    assert out["handled"][0]["kind"] == "carrier"
    assert armer.calls == [] and io.pushes == []
    assert io.store().projects[proj.id].exposure_overrides is None
    ev = _pilot_events(cfg, "carrier")
    assert len(ev) == 1 and "shortened RC16 subs 300 -> 180 s" in ev[0]["detail"]
    assert not _pilot_events(cfg, "act") and not npil.load_state(cfg, NIGHT).get("actions")
    # the same again later: one run event a night, still nothing sent
    _diag(cfg, "05:00", "tracking", "shorten_subs:180", ecc=0.8, exp_s=300,
          shared_mount=True)
    _tick(cfg, armer, io, "05:01")
    assert armer.calls == [] and io.pushes == [] and len(_pilot_events(cfg, "carrier")) == 1


def test_pilot_af_and_human_on_carrier_no_action_no_push(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_af_direct=False)
    armer, io = _carrier_armer(), FakeIO(cfg, phd2="Stopped")
    for hm in ("04:07", "04:12", "04:17"):
        _sub(cfg, hm, 0.45, hfr=14.0)
    _diag(cfg, "04:20", "focus", "run_af", confidence="medium", ecc=0.45,
          hfr_px=14.0, other_rig_sharp=True)
    _tick(cfg, armer, io, "04:21")
    _diag(cfg, "04:40", "focuser", confidence="high")
    _tick(cfg, armer, io, "04:41")
    assert armer.calls == [] and io.pushes == []
    assert "nina1_autofocus" not in io.calls


def test_pilot_still_stops_phd2_on_an_unguided_carrier(tmp_path):
    """PHD2 moves the shared mount: the Piggy-600 trails too. Not quieted."""
    cfg = _cfg(tmp_path)
    armer, io = _carrier_armer(), FakeIO(cfg, phd2="Guiding")
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    out = _tick(cfg, armer, io, "04:24")
    assert out["handled"][0]["action"] == "stop_phd2"
    assert io.calls.count("phd2_stop") == 1
    assert io.pushes and io.pushes[-1][1].startswith("Night pilot: stopped PHD2")


def test_pilot_piggy_unaffected_on_a_carrier_night(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = _carrier_armer(), FakeIO(cfg, phd2="Stopped")
    _diag(cfg, "04:20", "focus", "run_af", confidence="medium", rig="piggyback",
          hfr_px=4.0, other_rig_sharp=True)
    out = _tick(cfg, armer, io, "04:21")
    assert out["handled"][0]["kind"] == "act"
    assert "piggy_af" in io.calls
    assert any("Piggy-600" in m for _p, m in io.pushes)


def test_guided_carrier_night_is_normal(tmp_path):
    """Guided, the RC16 Ha is bonus data: the pilot acts as on any target."""
    cfg = _cfg(tmp_path, night_pilot_af_direct=False)
    armer, io = _carrier_armer(guided=True), FakeIO(cfg, phd2="Guiding")
    for hm in ("04:07", "04:12", "04:17"):
        _sub(cfg, hm, 0.45, hfr=14.0)
    _diag(cfg, "04:20", "focus", "run_af", confidence="medium", ecc=0.45,
          hfr_px=14.0, other_rig_sharp=True)
    _tick(cfg, armer, io, "04:21")
    assert armer.calls == [("restart", "after_exposure")]
    assert "asked for an autofocus on the RC16" in io.pushes[-1][1]
    assert not _pilot_events(cfg, "carrier")


def test_quiet_off_acts_as_before(tmp_path):
    cfg = _cfg(tmp_path, plan_moon_fill_carrier_quiet=False)
    armer, io = _carrier_armer(), FakeIO(cfg, phd2="Stopped")
    _heart(io)
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "tracking", "shorten_subs:180", ecc=0.76, exp_s=300,
          shared_mount=True, trail={"rate_arcsec_min": 0.45})
    _tick(cfg, armer, io, "04:30")
    assert armer.calls == [("restart", "after_exposure")]


def test_ladder_skips_the_rc16_on_a_carrier(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, night_pilot_ladder_mode="advise")
    seen = []

    async def fake_rig(cfg_, armer, io, st, ctx, now, m, rig):
        seen.append(rig)
        return None
    monkeypatch.setattr(npil, "_ladder_rig", fake_rig)
    st = npil.load_state(cfg, NIGHT)
    ctx = {"target": "Heart Nebula", "piggy": True}
    asyncio.run(npil.ladder(cfg, _carrier_armer(), None, st, ctx, T("04:00")))
    assert seen == ["piggyback"]
    assert [e["step"] for e in read_jsonl(events_path(cfg, NIGHT))
            if e.get("value") == "ladder"] == ["carrier"]
    seen.clear()
    asyncio.run(npil.ladder(cfg, _carrier_armer(guided=True), None, st, ctx, T("04:00")))
    assert seen == ["rc16", "piggyback"]


# ------------------------------------------------------------- 3. sub doctor

def test_sub_doctor_push_kind_on_a_carrier(tmp_path):
    from photonscript.telescope_agent.sub_doctor_live import carrier_kind
    cfg = _cfg(tmp_path)
    _save_state(cfg)
    last = {"target": "Heart Nebula", "rig": "rc16"}
    assert carrier_kind(cfg, NIGHT, "rc16", "tracking", last, "info") is None
    assert carrier_kind(cfg, NIGHT, "rc16", "focus", last, "info") is None
    assert carrier_kind(cfg, NIGHT, "rc16", "guider_conflict", last, "act") == "act"
    assert carrier_kind(cfg, NIGHT, "rc16", "focuser", last, "act") == "info"
    assert carrier_kind(cfg, NIGHT, "piggyback", "focus", last, "info") == "info"
    assert carrier_kind(cfg, NIGHT, "rc16", "tracking", {"target": "M31"}, "info") == "info"
    # the Piggy-600's autofocus fails too: the focuser act stays
    append_jsonl(events_path(cfg, NIGHT), {"t": "2026-10-13T04:00:00Z",
                                           "rig": "piggyback", "kind": "af_stall"})
    assert carrier_kind(cfg, NIGHT, "rc16", "focuser", last, "act") == "act"
    _save_state(cfg, guided=True)
    assert carrier_kind(cfg, NIGHT, "rc16", "tracking", last, "info") == "info"


def test_sub_doctor_after_sub_records_but_does_not_push(tmp_path, monkeypatch):
    from photonscript.scheduler import sub_doctor as sd
    from photonscript.telescope_agent import sub_doctor_live as live
    cfg = _cfg(tmp_path)
    _save_state(cfg)
    _tonight_bad_subs(cfg)
    diag = {"cause": "focus", "confidence": "high", "headline": "RC16 soft",
            "fix": "AF", "t": "2026-10-13T04:22:00Z", "file": None,
            "action": {"id": "run_af"}, "evidence": {}, "secondary": []}
    monkeypatch.setattr(sd, "diagnose", lambda *a, **k: dict(diag))
    pushes = []

    async def fake_notify(config, message, **kw):
        pushes.append((message, kw))
    monkeypatch.setattr("photonscript.shared.pushover.notify", fake_notify)
    ag = SimpleNamespace(config=cfg, rig="rc16", state=SimpleNamespace(guiding=None),
                         _af_reports={}, _focuser_info={})
    d = asyncio.run(live.after_sub(ag, NIGHT))
    assert d["cause"] == "focus" and pushes == []
    assert sd.load_state(cfg, NIGHT)["rigs"]["rc16"]["current"]["cause"] == "focus"
    assert [e for e in read_jsonl(events_path(cfg, NIGHT)) if e.get("kind") == "sub_doctor"]
    # the Piggy-600 the same night: pushed as before
    ag.rig = "piggyback"
    for hm in ("04:07", "04:12"):
        _sub(cfg, hm, 0.7, rig="piggyback")
    asyncio.run(live.after_sub(ag, NIGHT))
    assert len(pushes) == 1 and pushes[0][1]["kind"] == "info"


def test_sub_doctor_morning_line_skips_carrier_time(tmp_path):
    from photonscript.scheduler import sub_doctor as sd
    cfg = _cfg(tmp_path)
    _save_state(cfg)
    assert sd._carrier_fired(cfg, NIGHT, "rc16", {"cause": "tracking",
                                                  "t": "2026-10-13T04:00:00Z"})
    assert not sd._carrier_fired(cfg, NIGHT, "rc16", {"cause": "guider_conflict",
                                                      "t": "2026-10-13T04:00:00Z"})
    assert not sd._carrier_fired(cfg, NIGHT, "rc16", {"cause": "tracking",
                                                      "t": "2026-10-13T01:00:00Z"})
    assert not sd._carrier_fired(cfg, NIGHT, "piggyback", {"cause": "focus",
                                                           "t": "2026-10-13T04:00:00Z"})


# ------------------------------------------------------------- 4. telescope agent

def _agent(cfg, rig="rc16"):
    from photonscript.telescope_agent import agent as agent_mod
    a = agent_mod.TelescopeAgent.__new__(agent_mod.TelescopeAgent)
    a.config, a.rig = cfg, rig
    return a


def test_reject_streak_skips_carrier_subs(tmp_path):
    cfg = _cfg(tmp_path)
    _save_state(cfg)
    assert _agent(cfg)._carrier_sub("Heart Nebula", NIGHT) is True
    assert _agent(cfg)._carrier_sub("M31", NIGHT) is False
    assert _agent(cfg, "piggyback")._carrier_sub("Heart Nebula", NIGHT) is False
    _save_state(cfg, guided=True)
    assert _agent(cfg)._carrier_sub("Heart Nebula", NIGHT) is False
    src = (ROOT / "photonscript/telescope_agent/agent.py").read_text(encoding="utf-8")
    i = src.index("elif self._carrier_sub(target_name, night):")
    assert src.index("if is_test_target(target_name):") < i < \
        src.index("self._consecutive_rejects += 1", i)
    assert "_consecutive_rejects" not in src[i:src.index("elif quality.passed_qa:", i)]


def test_rc16_af_stall_is_info_on_a_carrier_unless_the_piggy_fails(tmp_path, monkeypatch):
    from photonscript.scheduler import af_stall
    from photonscript.telescope_agent import agent as agent_mod
    cfg = _cfg(tmp_path)
    _save_state(cfg)
    sent, held = [], []

    async def fake_notify(config, msg, **kw):
        sent.append((msg, kw))
    monkeypatch.setattr(agent_mod, "notify", fake_notify)
    monkeypatch.setattr("photonscript.shared.pushover.record",
                        lambda c, m, **kw: held.append((m, kw)))

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return T("04:00")
    monkeypatch.setattr(agent_mod, "datetime", _DT)
    monkeypatch.setattr(af_stall, "alert_text", lambda *a, **k: "RC16: autofocus stalled")
    monkeypatch.setattr(af_stall, "summary", lambda s: {})
    a = _agent(cfg)
    asyncio.run(a._af_stall_alarm([{}], {}, NIGHT, 4))
    assert sent == [] and held and held[0][1]["reason"] == "carrier-not-judged"
    # the Piggy-600 fails too: the RC16 act goes out
    append_jsonl(events_path(cfg, NIGHT), {"t": "2026-10-13T03:50:00Z",
                                           "rig": "piggyback", "kind": "af_stall"})
    asyncio.run(a._af_stall_alarm([{}], {}, NIGHT, 4))
    assert len(sent) == 1 and sent[0][1]["kind"] == "act"
    # the Piggy-600's own alarm is never quieted
    p = _agent(cfg, "piggyback")
    asyncio.run(p._af_stall_alarm([{}], {}, NIGHT, 4))
    assert len(sent) == 2 and sent[1][1]["kind"] == "act"


# ------------------------------------------------------------- 5. AF reports, focuser guard

def test_af_report_grader_quiet_for_rc16_carrier_runs(tmp_path):
    from photonscript.scheduler import focus_reports as fr
    cfg = _cfg(tmp_path)
    _save_state(cfg)
    rc = {"rig": "rc16", "timestamp": "2026-10-12T22:00:00-06:00", "ok": False}
    pig = {"rig": "piggyback", "timestamp": "2026-10-12T22:00:00-06:00", "ok": False}
    early = {"rig": "rc16", "timestamp": "2026-10-12T18:00:00-06:00", "ok": False}
    assert fr._carrier_kind(cfg, NIGHT, [rc]) == "info"
    assert fr._carrier_kind(cfg, NIGHT, [rc, pig]) == "act"
    assert fr._carrier_kind(cfg, NIGHT, [early]) == "act"      # before the carrier
    assert fr._carrier_kind(cfg, "2026-10-01", [rc]) == "act"   # no carrier that night


def test_focuser_guard_waits_on_a_carrier_then_judges_real_subs(tmp_path):
    from photonscript.scheduler import focuser_guard as fg
    from tests.test_scheduler import test_focuser_guard as tfg
    cfg = tfg._cfg(tmp_path)
    night = tfg.NIGHT
    armer = tfg.FakeArmer()
    armer.plan["moon_fill"] = _mf(target="M33", start="2026-10-25T02:00:00Z",
                                  end="2026-10-25T06:00:00Z")
    io = tfg.FakeIO([tfg.foc(5674), tfg.foc(0)])
    io.af_points = tfg._points()
    tfg.run(cfg, io, tfg.T("05:30"), armer=armer)
    tfg.run(cfg, io, tfg.T("05:41"), armer=armer)
    # a trailed carrier sub, then 30 min: no verdict, nothing moved
    io.sub_list = [tfg._sub(tfg.T("05:00"), 2.3, True), tfg._sub(tfg.T("05:45"), 8.3, False)]
    tfg.run(cfg, io, tfg.T("05:49"), armer=armer)
    tfg.run(cfg, io, tfg.T("05:59"), armer=armer)
    assert not [c for c in io.calls if c[0] in ("move", "autofocus")]
    assert [e["value"] for e in read_jsonl(events_path(cfg, night))
            if e.get("kind") == fg.KIND] == ["carrier"]
    # only the detection act (a reading, not a sub): kept as before
    assert [p["title"] for p in io.pushes if p["kind"] == "act"] == [fg.TITLE]
    # off the carrier (06:00Z): a real sub decides; the carrier subs do not
    real = tfg._sub(tfg.T("06:05"), 2.4, True)
    real["target"] = "M31"
    io.sub_list.append(real)
    tfg.run(cfg, io, tfg.T("06:10"), armer=armer)
    r = fg.resets(cfg, night, "rc16")[-1]
    assert r["verdict"] == "sharp"
    assert [c for c in io.calls if c[0] in ("move", "autofocus")] == [("autofocus", "rc16")]


# ------------------------------------------------------------- 6. reports

def _carrier_night_subs(cfg):
    for i, hm in enumerate(("03:00", "03:06", "03:12", "03:18")):
        _sub(cfg, hm, 0.8, hfr=6.0)                       # RC16 Ha, trailed
    _sub(cfg, "03:05", 0.4, hfr=3.0, rig="piggyback", passed=True, exp=450)
    _sub(cfg, "03:13", 0.4, hfr=3.0, rig="piggyback", passed=True, exp=450)


def test_morning_report_says_carrier_not_judged(tmp_path):
    cfg = _cfg(tmp_path)
    _save_state(cfg, state="COMPLETE")
    (Path(cfg.data_dir) / "runs").mkdir(parents=True, exist_ok=True)
    (Path(cfg.data_dir) / "runs" / f"{NIGHT}_plan.json").write_text(
        json.dumps({"night_of": NIGHT, "moon_fill": _mf()}), encoding="utf-8")
    (Path(cfg.data_dir) / "armer_state.json").unlink()
    _carrier_night_subs(cfg)
    card = mr.report_card(cfg, NIGHT, projects=[], calibration=False,
                          completeness=False)
    lines = card["lines"]
    assert "RC16 carrier for the Piggy-600, 4 subs, not judged (Heart Nebula)" in lines
    assert not any(ln.startswith("RC16: ") for ln in lines)
    assert any(ln.startswith("Piggy-600: 2 kept / 0 rejected") for ln in lines)
    assert mr.card_line_priority(lines[0]) == mr.PRI_RIG
    assert all(EM not in ln for ln in lines)


def test_morning_report_guided_carrier_is_normal(tmp_path):
    cfg = _cfg(tmp_path)
    (Path(cfg.data_dir) / "runs").mkdir(parents=True, exist_ok=True)
    (Path(cfg.data_dir) / "runs" / f"{NIGHT}_plan.json").write_text(
        json.dumps({"night_of": NIGHT, "moon_fill": _mf(guided=True)}), encoding="utf-8")
    _carrier_night_subs(cfg)
    card = mr.report_card(cfg, NIGHT, projects=[], calibration=False,
                          completeness=False)
    assert any(ln.startswith("RC16: 0 kept / 4 rejected") for ln in card["lines"])
    assert not any("not judged" in ln for ln in card["lines"])


def test_end_of_night_report_and_target_switch(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    _carrier_night_subs(cfg)
    from photonscript.scheduler.runs import _load_subs
    subs = _load_subs(cfg, NIGHT)
    plan = {"night_of": NIGHT, "moon_fill": _mf()}
    rep = ns.end_of_night_report(cfg, NIGHT, reason="dawn", subs=subs, projects=[],
                                 plan=plan, now=T("12:30"))
    assert "RC16 carrier for the Piggy-600, 4 subs, not judged (Heart Nebula)" in rep["lines"]
    assert rep["lines"][0].startswith(f"Night {NIGHT} over (dawn") and \
        "2 kept of 2 subs" in rep["lines"][0]
    assert not any(ln.startswith("RC16: ") or ln.startswith("RC16 stars") for ln in rep["lines"])
    assert any(ln.startswith("Piggy-600: Heart Nebula 2 shot, 2 kept") for ln in rep["lines"])
    # guided: the usual tally
    g = ns.end_of_night_report(cfg, NIGHT, reason="dawn", subs=subs, projects=[],
                               plan={"night_of": NIGHT, "moon_fill": _mf(guided=True)},
                               now=T("12:30"))
    assert any(ln.startswith("RC16: Heart Nebula 4 shot, 0 kept, 4 rej") for ln in g["lines"])
    # the RC16 leaves the carrier: the switch push does not tally rejects
    _save_state(cfg)
    pushes = []

    async def fake_push(config, msg, **kw):
        pushes.append(msg)
        return True
    monkeypatch.setattr(ns, "_push", fake_push)
    rec = {"rig": "rc16", "target": "Heart Nebula", "time": "2026-10-13T03:18:00Z",
           "passed_qa": False, "filter": "Ha", "exp_s": 300}
    asyncio.run(ns.after_sub(cfg, NIGHT, rec))
    asyncio.run(ns.after_sub(cfg, NIGHT, {**rec, "target": "M31", "filter": "L",
                                          "time": "2026-10-13T11:00:00Z"}))
    assert pushes and "after Heart Nebula: RC16 carrier for the Piggy-600, 4 subs, " \
        "not judged." in pushes[-1]
    assert "rejected" not in pushes[-1]
