"""Night pilot: the sub doctor's diagnoses turned into bounded, verified
adjustments (2026-10-12 night: a stray PHD2 session on a FAIL calibration
trailed every RC16 sub of an UNGUIDED night; 300 s trailed; a restart race
needed a manual retry).

  1. the adapter (the sub doctor's run event schema) and the policy table;
  2. replay of tonight: guider conflict -> stop PHD2 -> subs improve;
  3. trailing 300 s -> 180 s with verification; a step that did not
     help is undone (trailing is not drift-per-second), a human;
  4. the restart race: one retry once NINA #1 reads idle;
  5. focus soft on one rig -> AF (RC16 restart, Piggy-600 companion), AF
     failing twice -> push; both rigs soft -> nothing;
  6. guardrails: limits, cooldowns, TPoint / WATCHING / calibration capture
     / PHD2 calibrating / arm recipe;
  7. advise mode and per-action switches send no command;
  8. dawn revert (and a goal changed by hand is left alone), stale nights;
  9. morning report, dashboard status, config fields, wiring.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import morning_report as mr
from photonscript.scheduler import night_pilot as npil
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import append_jsonl, read_jsonl

ROOT = Path(__file__).resolve().parents[2]
NIGHT = "2026-10-12"
HEART = CelestialTarget(name="Heart Nebula", catalog_id="IC 1805",
                        ra_hours=2.55, dec_degrees=61.5,
                        object_type="emission nebula")


def T(hm: str) -> datetime:
    h, m = hm.split(":")
    return datetime(2026, 10, 13, int(h), int(m), 0)


def _cfg(tmp_path, **kw):
    kw.setdefault("observatory_tz", "America/Denver")
    kw.setdefault("piggyback_enabled", True)
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


class FakeArmer:
    def __init__(self, state="RUNNING", guided=False, dawn=T("12:00")):
        self.state = state
        self.plan = {"night_of": NIGHT}
        self.guided = guided
        self.dawn = dawn
        self.calls = []
        self.last_restart = None
        self.detail = ""
        self.recipe = None
        self.restart_result = {"ok": True, "pending": True}

    def _use_guiding(self):
        return self.guided

    def _dawn(self):
        return self.dawn

    def _recipe_tonight(self):
        return self.recipe

    async def restart(self, when="after_exposure", now=None):
        self.calls.append(("restart", when))
        return dict(self.restart_result)

    async def fallback_unguided(self, reason):
        self.calls.append(("fallback_unguided", reason))
        return True

    async def _dispatch_piggyback_companion(self):
        self.calls.append(("companion",))


class FakeIO(npil.PilotIO):
    def __init__(self, cfg, running=None, target="Heart Nebula", phd2="Guiding"):
        self.cfg = cfg
        self.running = running if running is not None else [
            "Heart Nebula", "Ha filter block (cooler-gated)", "Smart Exposure"]
        self.target = target
        self.phd2 = phd2
        self.calls = []
        self.pushes = []
        self.af_ok = True
        self._store = ProjectStore(cfg)

    async def nina1_running(self, cfg):
        self.calls.append("nina1_running")
        return self.running, self.target

    async def phd2_state(self, cfg):
        return self.phd2

    async def phd2_stop(self, cfg):
        self.calls.append("phd2_stop")
        self.phd2 = "Stopped"
        return True, "PHD2 now Stopped"

    async def nina1_wait_idle(self, cfg, max_s=300.0):
        self.calls.append("nina1_wait_idle")
        return True

    async def piggy_af(self, cfg, armer):
        self.calls.append("piggy_af")
        return self.af_ok, "NINA #2 stopped after the current sub; companion re-dispatched"

    def store(self):
        return self._store

    async def notify(self, cfg, msg, priority=0):
        self.pushes.append((priority, msg))


def _sub(cfg, hm, ecc, hfr=10.0, exp=300.0, rig="rc16", target="Heart Nebula",
         passed=False, n=[0]):
    n[0] += 1
    st = T(hm)
    rec = {"rig": rig, "file": f"{rig}/Heart/LIGHT/sub_{n[0]:04d}.fits",
           "target": target, "filter": "Ha" if rig == "rc16" else "OSC",
           "exp_s": exp, "start_utc": st.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "end_utc": (st + timedelta(seconds=exp)).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "ecc": ecc, "hfr": hfr, "passed_qa": passed}
    p = Path(cfg.data_dir) / "runs" / f"{NIGHT}_subs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")
    return rec


def _diag(cfg, hm, cause, action="none", confidence="high", rig="rc16", **ev):
    """A run event in the sub doctor's schema (sub_doctor.event_record)."""
    row = {"t": T(hm).strftime("%Y-%m-%dT%H:%M:%SZ"), "rig": rig,
           "src": "photonscript", "kind": "sub_doctor", "value": cause,
           "cause": cause, "confidence": confidence,
           "headline": f"{rig} {cause}: ecc {ev.get('ecc')}",
           "evidence": {"window": 3, "bad": 3, **ev}, "fix": "...",
           "suggested_action": {"id": action, "text": action, "auto": True,
                                "enabled": False},
           "secondary": [], "file": None, "n_window": 3, "n_bad": 3}
    append_jsonl(events_path(cfg, NIGHT), row)
    return row


def _pilot_events(cfg, value=None):
    return [r for r in read_jsonl(events_path(cfg, NIGHT))
            if r.get("kind") == "night_pilot" and (value is None or r.get("value") == value)]


def _tick(cfg, armer, io, hm):
    return asyncio.run(npil.tick(cfg, armer, io, now=T(hm)))


def _heart(io):
    proj = io.store().add_from_target(HEART, budget_hours=5.0)
    return proj


# ------------------------------------------------------------- 1. adapter

def test_norm_diag_sub_doctor_schema():
    row = {"t": "2026-10-13T04:23:00Z", "rig": "rc16", "kind": "sub_doctor",
           "value": "tracking", "cause": "tracking", "confidence": "medium",
           "evidence": {"ecc": 0.76}, "suggested_action": {"id": "shorten_subs:180"}}
    d = npil.norm_diag(row)
    assert d["family"] == "tracking" and d["action"] == "shorten_subs"
    assert d["action_arg"] == "180" and d["confidence"] == "medium"
    assert d["dt"] == datetime(2026, 10, 13, 4, 23)
    # numeric confidence, bare string action, value-only cause
    d = npil.norm_diag({"t": "2026-10-13T04:23:00Z", "kind": "sub_doctor",
                        "value": "focus", "confidence": 0.9,
                        "suggested_action": "run_af"})
    assert (d["family"], d["confidence"], d["action"], d["rig"]) == \
        ("focus", "high", "run_af", "rc16")
    assert npil.norm_diag({"kind": "sub_doctor_remedy", "t": "x"}) is None
    assert npil.norm_diag({"kind": "sub_doctor", "t": None}) is None


def test_ladder_and_policy_table(tmp_path):
    cfg = _cfg(tmp_path)
    assert npil.sub_steps(cfg) == [300.0, 180.0, 120.0]
    assert npil.next_step(cfg, 300) == 180 and npil.next_step(cfg, 180) == 120
    assert npil.next_step(cfg, 120) is None and npil.next_step(cfg, 600) == 300
    assert npil.next_step(_cfg(tmp_path, night_pilot_sub_floor_s=180), 180) is None
    assert set(npil.POLICY) == {"stop_phd2", "unguided", "shorten_subs", "run_af",
                                "skip_target"}
    for p in npil.POLICY.values():
        assert {"key", "max", "cooldown_min", "metric", "restart"} <= set(p)
        assert hasattr(cfg, p["key"])
    ctx = {"guided": False, "phd2": "Guiding", "exp_s": 300}

    def d(cause, action="none", conf="high", rig="rc16", **ev):
        return npil.norm_diag({"t": "2026-10-13T04:00:00Z", "kind": "sub_doctor",
                               "rig": rig, "cause": cause, "confidence": conf,
                               "evidence": ev, "suggested_action": {"id": action}})
    assert npil.decide(cfg, d("guider_conflict", "stop_phd2"), ctx)["action"] == "stop_phd2"
    assert npil.decide(cfg, d("guider_conflict"), {**ctx, "phd2": "Stopped"})["kind"] == "annotate"
    g = {**ctx, "guided": True}
    assert npil.decide(cfg, d("guider_conflict", "recalibrate_phd2",
                              calibration={"grade": "FAIL"}), g)["action"] == "unguided"
    assert npil.decide(cfg, d("guider_conflict", "reselect_guide_star"), g)["kind"] == "human"
    t = npil.decide(cfg, d("tracking", shared_mount=True), ctx)
    assert (t["action"], t["params"]["from_s"], t["params"]["to_s"]) == ("shorten_subs", 300, 180)
    assert npil.decide(cfg, d("tracking", trail={"rate_arcsec_min": 0.3}),
                       ctx)["action"] == "shorten_subs"
    assert npil.decide(cfg, d("tracking"), ctx)["kind"] == "annotate"
    assert npil.decide(cfg, d("tracking", shared_mount=True), g)["kind"] == "human"
    assert npil.decide(cfg, d("tracking", shared_mount=True),
                       {**ctx, "exp_s": 120})["kind"] == "human"
    assert npil.decide(cfg, d("focus", "run_af", other_rig_sharp=True), ctx)["action"] == "run_af"
    assert npil.decide(cfg, d("focus", other_rig_sharp=False), ctx)["kind"] == "annotate"
    assert npil.decide(cfg, d("pointing", plate_solve_errors=3), ctx)["action"] == "skip_target"
    assert npil.decide(cfg, d("pointing", plate_solve_errors=1), ctx)["kind"] == "annotate"
    for c in ("clouds", "seeing", "other", "none"):
        assert npil.decide(cfg, d(c), ctx)["kind"] == "annotate"
    assert npil.decide(cfg, d("focuser"), ctx)["kind"] == "human"
    assert npil.decide(cfg, d("guider_conflict", "stop_phd2", conf="low"), ctx)["kind"] == "annotate"


def test_pick_step_prefers_matched_darks_then_bias_scaled(tmp_path):
    cfg = _cfg(tmp_path)

    def st(table):
        return lambda s: {"match": table.get(s, "none"), "text": f"{s:g}:{table.get(s)}"}
    # 180 s darks: the next step
    s, dk = npil.pick_step(cfg, 300, st({180.0: "matched", 120.0: "matched"}))
    assert (s, dk["match"]) == (180.0, "matched")
    # only 120 s darks: skip to 120 rather than 180 s without darks
    s, dk = npil.pick_step(cfg, 300, st({180.0: "scaled", 120.0: "matched"}))
    assert (s, dk["match"]) == (120.0, "matched")
    # no matched darks: the longest length a bias can scale to
    s, dk = npil.pick_step(cfg, 300, st({180.0: "none", 120.0: "scaled"}))
    assert (s, dk["match"]) == (120.0, "scaled")
    # nothing at all: the next step, said so
    s, dk = npil.pick_step(cfg, 300, st({}))
    assert (s, dk["match"]) == (180.0, "none")
    assert npil.pick_step(cfg, 120, st({})) == (None, None)
    # decide() carries it into the params (the push text)
    d = npil.norm_diag({"t": "2026-10-13T04:00:00Z", "kind": "sub_doctor",
                        "cause": "tracking", "confidence": "high",
                        "evidence": {"shared_mount": True}})
    dec = npil.decide(cfg, d, {"guided": False, "exp_s": 300,
                               "dark_status": st({120.0: "matched"})})
    assert dec["params"]["to_s"] == 120.0 and dec["params"]["darks"] == "matched"
    # the real library: 180 s is in the default RC16 dark quota (600,180 +
    # the 300 s unguided cap); 120 s has no darks and no bias here
    assert npil.dark_match(cfg, "rc16", 180)["match"] == "matched"
    assert "night dark quota" in npil.dark_match(cfg, "rc16", 180)["text"]
    assert npil.dark_match(cfg, "rc16", 120) == {
        "match": "none", "text": "no 120 s darks and no bias at this epoch: darks owed"}


def test_pick_step_bias_scaled_from_the_library(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration as cal
    monkeypatch.setattr(cal, "newest_usable_bias", lambda c, r="rc16": "2026-10-01")
    cfg = _cfg(tmp_path)
    s, dk = npil.pick_step(cfg, 180)
    assert s == 120.0 and dk["match"] == "scaled"
    assert dk["text"] == "no 120 s darks: scaled from the 2026-10-01 bias at this epoch"
    monkeypatch.setattr(cal, "darks_have", lambda c, r, e, **k: 25 if e == 120 else 0)
    assert npil.dark_match(cfg, "rc16", 120)["text"] == "darks match: 25 120 s darks in the library"


# ------------------------------------------------------------- 2. tonight replay

def _tonight_bad_subs(cfg):
    for hm, ecc in (("04:07", 0.757), ("04:12", 0.629), ("04:17", 0.779), ("04:22", 0.709)):
        _sub(cfg, hm, ecc, hfr=11.0)


def test_replay_guider_conflict_stop_phd2_subs_improve(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(guided=False), None
    io = FakeIO(cfg, phd2="Guiding")
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76, hfr_px=11.0,
          calibration={"grade": "FAIL"})
    out = _tick(cfg, armer, io, "04:24")
    assert out["handled"][0]["action"] == "stop_phd2"
    assert io.calls.count("phd2_stop") == 1 and armer.calls == []
    pri, msg = io.pushes[-1]
    assert msg.startswith("Night pilot: stopped PHD2 guiding because ")
    assert "next check" in msg and "local" in msg and pri == 0
    act = _pilot_events(cfg, "act")
    assert len(act) == 1 and act[0]["action"] == "stop_phd2"
    assert act[0]["baseline"] == {"metric": "ecc", "value": 0.76}
    # the sub that was running at the stop does not count; the next two do
    _sub(cfg, "04:22", 0.70)            # duplicate start before the action
    _sub(cfg, "04:27", 0.52, hfr=9.0)
    assert _tick(cfg, armer, io, "04:31")["handled"] == []
    assert not _pilot_events(cfg, "verify")
    _sub(cfg, "04:32", 0.50, hfr=9.0)
    _tick(cfg, armer, io, "04:38")
    v = _pilot_events(cfg, "verify")
    assert len(v) == 1 and v[0]["result"] == "improved"
    assert v[0]["before"] == 0.76 and v[0]["after"] == 0.51
    assert "worked: ecc 0.76 -> 0.51 on the next 2 subs" in io.pushes[-1][1]
    st = npil.load_state(cfg, NIGHT)
    assert st["actions"][0]["result"] == "improved"
    assert any("stop PHD2 before arming unguided" in x for x in st["learned"])
    # the same diagnosis again later: PHD2 is stopped now: nothing sent
    _diag(cfg, "04:40", "guider_conflict", "stop_phd2", ecc=0.55)
    io.phd2 = "Stopped"
    _tick(cfg, armer, io, "04:41")
    assert io.calls.count("phd2_stop") == 1


def test_stop_phd2_not_enough_pushes_but_stops_a_later_session(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="LostLock")
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", confidence="medium", ecc=0.76)
    _tick(cfg, armer, io, "04:24")
    _sub(cfg, "04:27", 0.74)
    _sub(cfg, "04:32", 0.75)
    _tick(cfg, armer, io, "04:38")
    pri, msg = io.pushes[-1]
    assert pri == 1 and "Stopping PHD2 was not enough" in msg
    assert not npil.load_state(cfg, NIGHT)["given_up"]
    # 08:30Z: PHD2 guiding again (both rigs dragged): stopped a second time
    io.phd2 = "Guiding"
    _diag(cfg, "08:37", "guider_conflict", "stop_phd2", ecc=0.86)
    _tick(cfg, armer, io, "08:38")
    assert io.calls.count("phd2_stop") == 2


def test_guided_fail_calibration_switches_to_unguided(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(guided=True), FakeIO(cfg)

    async def run():
        _diag(cfg, "04:23", "guider_conflict", "recalibrate_phd2", ecc=0.8,
              calibration={"grade": "FAIL", "reasons": ["axes 78 deg"]})
        await npil.tick(cfg, armer, io, now=T("04:24"))
        await asyncio.sleep(0)
        await npil.tick(cfg, armer, io, now=T("04:25"))
    asyncio.run(run())
    assert armer.calls and armer.calls[0][0] == "fallback_unguided"
    assert "phd2_stop" not in io.calls
    st = npil.load_state(cfg, NIGHT)
    assert st["actions"][0]["status"] == "done"
    assert st["actions"][0]["effective_t"] == "2026-10-13T04:25:00Z"
    assert _pilot_events(cfg, "dispatched")


# ------------------------------------------------------------- 3. trailing

def test_trailing_step_that_did_not_help_is_undone_then_dawn_revert(tmp_path):
    """2026-10-12 replay: 300 s trailed, 180 s trailed the same. The step is
    undone (back to the goal default, restart after the sub), trailing is
    recorded as not drift-per-second, and the night never goes to 120 s."""
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(guided=False), FakeIO(cfg, phd2="Stopped")
    proj = _heart(io)
    assert proj.exposure_overrides is None
    ha0 = next(p for p in proj.exposure_plans if p.filter_type.value == "Ha")
    assert ha0.exposure_seconds == 600
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "tracking", "shorten_subs:180", ecc=0.76, exp_s=300,
          shared_mount=True, trail={"rate_arcsec_min": 0.45})
    _tick(cfg, armer, io, "04:30")
    assert armer.calls == [("restart", "after_exposure")]
    p = io.store().projects[proj.id]
    assert p.exposure_overrides and set(p.exposure_overrides.values()) == {180.0}
    assert all(e.exposure_seconds <= 180 for e in p.exposure_plans if e.rig == "rc16")
    assert "shortened RC16 subs 300 -> 180 s" in io.pushes[-1][1]
    assert "Heart Nebula: Ha 600 -> 180 s" in io.pushes[-1][1]
    assert "darks match: 180 s is in the night dark quota" in io.pushes[-1][1]
    st = npil.load_state(cfg, NIGHT)
    assert st["actions"][0]["status"] == "pending"
    assert st["actions"][0]["params"]["prev_overrides"] is None
    assert st["ledger"][proj.id]["original_overrides"] is None
    # restart dispatched at 04:35 (NINA #1 stopped after the 300 s sub)
    armer.last_restart = {"at": "2026-10-13T04:35:30Z", "ok": True}
    _tick(cfg, armer, io, "04:36")
    st = npil.load_state(cfg, NIGHT)
    assert st["actions"][0]["status"] == "done"
    assert st["actions"][0]["effective_t"] == "2026-10-13T04:35:30Z"
    # 10-12 for real: 180 s subs still trailed (0.733, 0.723)
    _sub(cfg, "04:36", 0.733, exp=180)
    _sub(cfg, "04:39", 0.723, exp=180)
    _tick(cfg, armer, io, "04:43")
    v = _pilot_events(cfg, "verify")[-1]
    assert v["result"] == "not_improved" and v["human"] is True
    pri, msg = io.pushes[-1]
    assert pri == 1 and "not a drift-per-second problem" in msg
    assert ("Heart Nebula: sub lengths back to the goal defaults (restart after "
            "the current sub)") in msg
    assert "No more shortening tonight" in msg
    # the cap is gone and the restart that applies it was asked for
    assert io.store().projects[proj.id].exposure_overrides is None
    assert armer.calls == [("restart", "after_exposure")] * 2
    rv = _pilot_events(cfg, "revert")
    assert rv and rv[-1]["detail"].startswith("undid 'shortened RC16 subs 300 -> 180 s'")
    a = npil.load_state(cfg, NIGHT)["actions"][0]
    assert a["undo"]["ok"] and a["undo"]["restart"]["ok"]
    # the next trailing diagnoses: given up, nothing goes to 120 s
    for hm in ("04:47", "05:20"):
        _diag(cfg, hm, "tracking", ecc=0.73, exp_s=300, shared_mount=True)
        _tick(cfg, armer, io, hm)
    assert len(armer.calls) == 2
    assert io.store().projects[proj.id].exposure_overrides is None
    st = npil.load_state(cfg, NIGHT)
    assert st["given_up"].get("rc16|tracking")
    assert any(x.startswith("RC16 trailing is not drift-per-second") and
               "put back to 300 s" in x for x in st["learned"])
    # dawn: nothing left to change, the goal settings as they were
    txt = npil.dawn_revert(cfg, NIGHT, store=io.store(), now=T("12:30"))
    assert txt.startswith("night pilot restored Heart Nebula: sub lengths restored")
    p = io.store().projects[proj.id]
    assert p.exposure_overrides is None
    assert next(e for e in p.exposure_plans if e.filter_type.value == "Ha").exposure_seconds == 600
    assert npil.dawn_revert(cfg, NIGHT, store=io.store()) is None   # once
    lines = npil.morning_lines(cfg, NIGHT)
    assert lines[0].startswith("Night pilot: ")
    assert "shortened RC16 subs 300 -> 180 s -> did not help (ecc 0.76 -> 0.728)" in lines[0]
    assert "Night pilot restored tonight-only changes: Heart Nebula" in lines
    assert any(x.startswith("Night pilot learned: RC16 trailing is not drift-per-second")
               for x in lines)


def test_second_step_that_did_not_help_goes_back_to_the_first(tmp_path):
    """300 -> 180 s helped; later 180 -> 120 s did not: back to 180 s."""
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")
    proj = _heart(io)
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg, armer, io, "04:30")
    armer.last_restart = {"at": "2026-10-13T04:35:00Z", "ok": True}
    _tick(cfg, armer, io, "04:35")
    _sub(cfg, "04:36", 0.52, exp=180)
    _sub(cfg, "04:39", 0.50, exp=180)
    _tick(cfg, armer, io, "04:43")
    assert npil.load_state(cfg, NIGHT)["actions"][0]["result"] == "improved"
    cap180 = dict(io.store().projects[proj.id].exposure_overrides)
    assert set(cap180.values()) == {180.0}
    # later in the night 180 s trails too: one more step
    _diag(cfg, "05:30", "tracking", ecc=0.70, exp_s=180, shared_mount=True)
    _tick(cfg, armer, io, "05:30")
    assert set(io.store().projects[proj.id].exposure_overrides.values()) == {120.0}
    a2 = npil.load_state(cfg, NIGHT)["actions"][1]
    assert a2["params"]["prev_overrides"] == cap180
    armer.last_restart = {"at": "2026-10-13T05:33:00Z", "ok": True}
    _tick(cfg, armer, io, "05:33")
    _sub(cfg, "05:34", 0.71, exp=120)
    _sub(cfg, "05:36", 0.70, exp=120)
    _tick(cfg, armer, io, "05:39")
    assert io.store().projects[proj.id].exposure_overrides == cap180
    assert "sub lengths back to " in io.pushes[-1][1] and "180 s" in io.pushes[-1][1]
    assert len(armer.calls) == 3    # two steps and the undo, nothing more


def test_undo_without_dark_left_restores_for_the_next_dispatch(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")
    proj = _heart(io)
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg, armer, io, "04:30")
    armer.last_restart = {"at": "2026-10-13T04:35:00Z", "ok": True}
    _tick(cfg, armer, io, "04:35")
    _sub(cfg, "04:36", 0.75, exp=180)
    _sub(cfg, "04:39", 0.76, exp=180)
    armer.dawn = T("05:00")            # 17 min of dark left at the check
    _tick(cfg, armer, io, "04:43")
    assert io.store().projects[proj.id].exposure_overrides is None
    assert len(armer.calls) == 1        # no restart for the undo
    assert "from the next dispatch; no restart now: only 17 min of dark left" \
        in io.pushes[-1][1]
    # a goal edited by hand since the cap is never undone
    cfg2 = _cfg(tmp_path / "b")
    armer2, io2 = FakeArmer(), FakeIO(cfg2, phd2="Stopped")
    p2 = _heart(io2)
    _diag(cfg2, "04:23", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg2, armer2, io2, "04:30")
    armer2.last_restart = {"at": "2026-10-13T04:35:00Z", "ok": True}
    _tick(cfg2, armer2, io2, "04:35")
    io2.store().update(p2.id, exposure_overrides={"Ha": 240})
    _sub(cfg2, "04:36", 0.75, exp=180)
    _sub(cfg2, "04:39", 0.76, exp=180)
    _tick(cfg2, armer2, io2, "04:43")
    assert io2.store().projects[p2.id].exposure_overrides == {"Ha": 240.0}
    assert "sub lengths NOT restored" in io2.pushes[-1][1]
    assert len(armer2.calls) == 1


def test_trailing_improves_learns_sub_length(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")
    _heart(io)
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg, armer, io, "04:30")
    armer.last_restart = {"at": "2026-10-13T04:35:00Z", "ok": True}
    _tick(cfg, armer, io, "04:35")
    _sub(cfg, "04:36", 0.52, exp=180)
    _sub(cfg, "04:39", 0.50, exp=180)
    _sub(cfg, "04:37", 0.80, exp=300)   # a 300 s sub never counts for a 180 s cap
    _tick(cfg, armer, io, "04:43")
    st = npil.load_state(cfg, NIGHT)
    assert st["actions"][0]["result"] == "improved"
    assert st["actions"][0]["after"]["value"] == 0.51
    assert any(x.startswith("unguided RC16 needs <= 180 s") for x in st["learned"])


# ------------------------------------------------------------- 4. restart race

def test_restart_race_one_retry_after_nina_idle(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")
    _heart(io)
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg, armer, io, "04:30")
    # 10-13: load/start ran before NINA #1 wound down: dispatch failed, paused
    armer.state = "PAUSED_OPERATOR"
    armer.last_restart = {"at": "2026-10-13T04:35:00Z", "ok": False,
                          "detail": "Restart FAILED: Sequence is already running"}
    armer.restart_result = {"ok": True, "redispatched": True}
    _tick(cfg, armer, io, "04:36")
    assert "nina1_wait_idle" in io.calls
    assert armer.calls == [("restart", "after_exposure"), ("restart", "after_exposure")]
    st = npil.load_state(cfg, NIGHT)
    assert st["actions"][0]["status"] == "done"
    assert st["actions"][0]["restart"]["retried"] is True
    assert _pilot_events(cfg, "retry")


def test_restart_fails_twice_pushes_human(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")
    _heart(io)
    _diag(cfg, "04:23", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg, armer, io, "04:30")
    armer.last_restart = {"at": "2026-10-13T04:35:00Z", "ok": False, "detail": "busy"}
    _tick(cfg, armer, io, "04:36")      # retry -> pending again
    armer.last_restart = {"at": "2026-10-13T04:41:00Z", "ok": False, "detail": "busy"}
    _tick(cfg, armer, io, "04:42")
    st = npil.load_state(cfg, NIGHT)
    assert st["actions"][0]["status"] == "failed"
    assert io.pushes[-1][0] == 1 and "FAILED to act" in io.pushes[-1][1]


# ------------------------------------------------------------- 5. focus

def test_focus_soft_one_rig_af_and_af_failing_twice(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")
    for hm in ("04:07", "04:12", "04:17"):
        _sub(cfg, hm, 0.45, hfr=14.0)
    _diag(cfg, "04:20", "focus", "run_af", confidence="medium", ecc=0.45,
          hfr_px=14.0, other_rig_sharp=True)
    _tick(cfg, armer, io, "04:21")
    assert armer.calls == [("restart", "after_exposure")]
    assert "asked for an autofocus on the RC16" in io.pushes[-1][1]
    armer.last_restart = {"at": "2026-10-13T04:25:00Z", "ok": True}
    _tick(cfg, armer, io, "04:25")
    _sub(cfg, "04:31", 0.45, hfr=13.8)
    _sub(cfg, "04:36", 0.45, hfr=13.9)
    _tick(cfg, armer, io, "04:42")
    assert "One more AF" in io.pushes[-1][1]
    _diag(cfg, "04:55", "focus", "run_af", confidence="medium", hfr_px=13.9,
          other_rig_sharp=True)
    _tick(cfg, armer, io, "04:56")
    assert len(armer.calls) == 2
    armer.last_restart = {"at": "2026-10-13T05:00:00Z", "ok": True}
    _tick(cfg, armer, io, "05:00")
    _sub(cfg, "05:05", 0.45, hfr=14.1)
    _sub(cfg, "05:10", 0.45, hfr=14.0)
    _tick(cfg, armer, io, "05:16")
    pri, msg = io.pushes[-1]
    assert pri == 1 and "Autofocus did not fix it twice" in msg
    assert npil.load_state(cfg, NIGHT)["given_up"].get("rc16|focus")


def test_focus_af_works_and_both_soft_is_seeing(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")
    _diag(cfg, "04:20", "focus", confidence="medium", hfr_px=14.0,
          other_rig_sharp=False)
    _tick(cfg, armer, io, "04:21")
    assert armer.calls == [] and io.pushes == []
    assert _pilot_events(cfg, "annotate")
    _diag(cfg, "04:30", "focus", "run_af", confidence="medium", hfr_px=14.0,
          other_rig_sharp=True)
    _tick(cfg, armer, io, "04:31")
    armer.last_restart = {"at": "2026-10-13T04:35:00Z", "ok": True}
    _tick(cfg, armer, io, "04:35")
    _sub(cfg, "04:40", 0.45, hfr=9.5)
    _sub(cfg, "04:45", 0.45, hfr=9.8)
    _tick(cfg, armer, io, "04:51")
    assert npil.load_state(cfg, NIGHT)["actions"][0]["result"] == "improved"


def test_piggy_focus_soft_runs_companion_af(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")

    async def run():
        _diag(cfg, "04:20", "focus", "run_af", rig="piggyback", confidence="medium",
              hfr_px=5.5, other_rig_sharp=True)
        await npil.tick(cfg, armer, io, now=T("04:21"))
        await asyncio.sleep(0)
        await npil.tick(cfg, armer, io, now=T("04:30"))
    asyncio.run(run())
    assert io.calls.count("piggy_af") == 1 and armer.calls == []
    a = npil.load_state(cfg, NIGHT)["actions"][0]
    assert a["rig"] == "piggyback" and a["status"] == "done"
    assert a["effective_t"] == "2026-10-13T04:30:00Z"


# ------------------------------------------------------------- 6. guardrails

@pytest.mark.parametrize("setup,why", [
    (lambda a, io: setattr(a, "state", "WATCHING"), "watching a sideloaded night"),
    (lambda a, io: setattr(a, "state", "PAUSED_OPERATOR"), "acts only while RUNNING"),
    (lambda a, io: setattr(a, "state", "ARMED"), "acts only while RUNNING"),
    (lambda a, io: setattr(io, "running", ["TPOINT_MAPPING", "TPoint point 12"]),
     "NINA #1 runs"),
    (lambda a, io: setattr(io, "running", ["Tracking test", "Take Exposure"]),
     "NINA #1 runs"),
    (lambda a, io: setattr(io, "running", ["PHD2_CALIBRATION"]), "NINA #1 runs"),
    (lambda a, io: setattr(io, "phd2", "Calibrating"), "PHD2 is calibrating"),
    (lambda a, io: setattr(a, "recipe", {"status": "started",
                                          "container": "TPoint mapping (60)"}),
     "arm recipe"),
    (lambda a, io: setattr(a, "dawn", T("04:50")), "min of dark left"),
])
def test_guards_hold_and_send_nothing(tmp_path, setup, why):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Guiding")
    _heart(io)
    setup(armer, io)
    _diag(cfg, "04:23", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg, armer, io, "04:24")
    assert armer.calls == [] and "phd2_stop" not in io.calls
    held = _pilot_events(cfg, "held")
    assert held and why in held[0]["detail"], held
    assert io.pushes == []
    assert io.store().projects[next(iter(io.store().projects))].exposure_overrides is None


def test_guard_calibration_capture(tmp_path, monkeypatch):
    from photonscript.scheduler import calibration_capture as cc
    monkeypatch.setattr(cc, "busy", lambda rig: rig == "piggyback")
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    _tick(cfg, armer, io, "04:24")
    assert "phd2_stop" not in io.calls
    assert "calibration capture job" in _pilot_events(cfg, "held")[0]["detail"]


def test_guard_target_named_dark_is_not_a_darks_block(tmp_path):
    cfg = _cfg(tmp_path)
    armer = FakeArmer()
    io = FakeIO(cfg, running=["Dark Shark Nebula", "Smart Exposure"],
                target="Dark Shark Nebula")
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    _tick(cfg, armer, io, "04:24")
    assert io.calls.count("phd2_stop") == 1


def test_max_actions_and_per_action_max(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_max_actions=1)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Guiding")
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    _tick(cfg, armer, io, "04:24")
    _sub(cfg, "04:26", 0.5)
    _sub(cfg, "04:31", 0.5)
    _tick(cfg, armer, io, "04:37")     # verified
    io.phd2 = "Guiding"                # PHD2 started again
    _diag(cfg, "05:30", "guider_conflict", "stop_phd2", ecc=0.76)
    _tick(cfg, armer, io, "05:31")
    assert io.calls.count("phd2_stop") == 1
    assert "1 actions tonight (max 1)" in _pilot_events(cfg, "held")[-1]["detail"]
    # per-action max (stop_phd2: 2 per night)
    cfg2 = _cfg(tmp_path / "b")
    io2 = FakeIO(cfg2, phd2="Guiding")
    for i, hm in enumerate(("01:00", "02:00", "03:00")):
        io2.phd2 = "Guiding"
        _diag(cfg2, hm, "guider_conflict", "stop_phd2", ecc=0.76)
        _tick(cfg2, armer, io2, hm)
        start = T(hm) + timedelta(minutes=2)
        _sub(cfg2, start.strftime("%H:%M"), 0.5)
        _sub(cfg2, (start + timedelta(minutes=5)).strftime("%H:%M"), 0.5)
        _tick(cfg2, armer, io2, (start + timedelta(minutes=11)).strftime("%H:%M"))
    assert io2.calls.count("phd2_stop") == 2
    assert "stop_phd2 done 2 time(s) tonight" in _pilot_events(cfg2, "held")[-1]["detail"]


def test_sub_doctor_remedy_not_doubled(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Guiding")
    append_jsonl(events_path(cfg, NIGHT), {
        "t": "2026-10-13T04:20:00Z", "rig": "rc16", "src": "photonscript",
        "kind": "sub_doctor_remedy", "value": "stop_phd2", "ok": True, "detail": "x"})
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    _tick(cfg, armer, io, "04:24")
    assert "phd2_stop" not in io.calls
    assert "sub doctor already ran stop_phd2" in _pilot_events(cfg, "held")[0]["detail"]


def test_stop_phd2_held_while_another_actor_holds_phd2(tmp_path):
    """One actor at a time: the PS-93 calibration manager (or any PS-91
    actor) holding PHD2 holds the pilot's stop; nothing is sent."""
    from photonscript.telescope_agent import phd2_ops
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Guiding")
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    phd2_ops._reset_for_tests()
    phd2_ops._try("calmanager")
    try:
        _tick(cfg, armer, io, "04:24")
    finally:
        phd2_ops._reset_for_tests()
    assert "phd2_stop" not in io.calls
    assert "holds PHD2 (calmanager)" in _pilot_events(cfg, "held")[0]["detail"]


def test_clouds_annotate_only(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    _diag(cfg, "04:23", "clouds", "wait_clouds", stars=12, both_rigs=True)
    _tick(cfg, armer, io, "04:24")
    _diag(cfg, "04:28", "clouds", "wait_clouds", stars=10, both_rigs=True)
    _tick(cfg, armer, io, "04:29")
    assert armer.calls == [] and io.pushes == [] and "phd2_stop" not in io.calls
    ann = _pilot_events(cfg, "annotate")
    assert len(ann) == 1 and "roof safety owns clouds" in ann[0]["detail"]


def test_focuser_dead_pushes_human_once(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    for hm in ("04:23", "04:30"):
        _diag(cfg, hm, "focuser", "check_focuser", focpos_zero_subs=3)
        _tick(cfg, armer, io, hm)
    assert armer.calls == [] and len(io.pushes) == 1
    assert io.pushes[0][1].startswith("Night pilot: needs a human: check the focuser")


def test_skip_target_once_and_revert(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Stopped")
    proj = _heart(io)
    _diag(cfg, "04:23", "pointing", "check_pointing", plate_solve_errors=4)
    _tick(cfg, armer, io, "04:24")
    assert armer.calls == [("restart", "after_exposure")]
    assert io.store().projects[proj.id].active is False
    armer.last_restart = {"at": "2026-10-13T04:30:00Z", "ok": True}
    _tick(cfg, armer, io, "04:30")
    _sub(cfg, "04:33", 0.5, target="Soul Nebula")
    _sub(cfg, "04:38", 0.5, target="Soul Nebula")
    _tick(cfg, armer, io, "04:44")
    assert npil.load_state(cfg, NIGHT)["actions"][0]["result"] == "improved"
    _diag(cfg, "05:23", "pointing", "check_pointing", plate_solve_errors=4)
    _tick(cfg, armer, io, "05:24")
    assert len(armer.calls) == 1        # once per night
    npil.dawn_revert(cfg, NIGHT, store=io.store())
    assert io.store().projects[proj.id].active is True


# ------------------------------------------------------------- 7. advise

def test_advise_mode_sends_no_command(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_mode="advise")
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Guiding")
    proj = _heart(io)
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    _tick(cfg, armer, io, "04:24")
    _diag(cfg, "04:50", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg, armer, io, "04:50")
    _diag(cfg, "05:10", "tracking", ecc=0.76, exp_s=300, shared_mount=True)
    _tick(cfg, armer, io, "05:10")
    assert "phd2_stop" not in io.calls and "piggy_af" not in io.calls
    assert armer.calls == []
    assert io.store().projects[proj.id].exposure_overrides is None
    msgs = [m for _p, m in io.pushes]
    assert len(msgs) == 2     # one per rig and action per night
    assert msgs[0].startswith("Night pilot (advise): would have stopped PHD2 guiding")
    assert "nothing was sent" in msgs[0]
    assert "would have shortened RC16 subs 300 -> 180 s" in msgs[1]
    assert "(darks match: 180 s is in the night dark quota)" in msgs[1]
    assert len(_pilot_events(cfg, "advise")) == 2 and not _pilot_events(cfg, "act")
    assert "would have" in npil.status(cfg, NIGHT)["line"]


def test_action_switch_off_is_advise(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_stop_phd2=False)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Guiding")
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    _tick(cfg, armer, io, "04:24")
    assert "phd2_stop" not in io.calls
    assert "night_pilot_stop_phd2 is off" in io.pushes[0][1]


def test_off_mode_does_nothing(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_mode="off")
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Guiding")
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    assert _tick(cfg, armer, io, "04:24") == {"state": "off"}
    assert io.calls == [] and io.pushes == [] and not _pilot_events(cfg)
    assert npil.mode(_cfg(tmp_path, night_pilot_mode="junk")) == "act"


# ------------------------------------------------------------- 8. revert

def test_revert_leaves_hand_edits_and_reverts_stale_nights(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO(cfg)
    proj = _heart(io)
    io.store().update(proj.id, exposure_overrides={"L": 120})
    st = npil.load_state(cfg, NIGHT)
    ok, txt = npil.apply_sub_cap(st, io.store(), "IC 1805", 180)
    assert ok and "Ha 600 -> 180 s" in txt and "L" not in txt
    npil.save_state(cfg, st)
    # Jeremy edits the goal by hand mid-night: the pilot leaves it
    io.store().update(proj.id, exposure_overrides={"Ha": 240})
    out = npil.revert(cfg, st, io.store(), why="test")
    assert "changed by hand since: left as they are" in out[0]
    assert io.store().projects[proj.id].exposure_overrides == {"Ha": 240.0}
    # an earlier night whose dawn shutdown never ran: reverted at the next tick
    cfg2 = _cfg(tmp_path / "b")
    io2 = FakeIO(cfg2)
    p2 = _heart(io2)
    st2 = npil.load_state(cfg2, "2026-10-11")
    npil.apply_sub_cap(st2, io2.store(), "Heart Nebula", 180)
    npil.save_state(cfg2, st2)
    _tick(cfg2, FakeArmer(), io2, "04:00")
    assert io2.store().projects[p2.id].exposure_overrides is None
    assert npil.load_state(cfg2, "2026-10-11")["ledger"][p2.id]["reverted"] is True
    # a night disarmed mid-way: reverted as soon as the armer is not live
    st3 = npil.load_state(cfg2, NIGHT)
    npil.apply_sub_cap(st3, io2.store(), "Heart Nebula", 180)
    npil.save_state(cfg2, st3)
    _tick(cfg2, FakeArmer(state="DISARMED"), io2, "05:00")
    assert io2.store().projects[p2.id].exposure_overrides is None


def test_dawn_shutdown_calls_revert():
    src = (ROOT / "photonscript/scheduler/armer.py").read_text(encoding="utf-8")
    body = src[src.index("async def dawn_shutdown"):src.index("async def _notify_complete")]
    assert "dawn_revert(self.config" in body
    assert body.index("self._persist()") < body.index("dawn_revert(")


def test_dawn_revert_without_state_is_quiet(tmp_path):
    cfg = _cfg(tmp_path)
    assert npil.dawn_revert(cfg, NIGHT, store=object()) is None
    assert npil.dawn_revert(cfg, None) is None


# ------------------------------------------------------------- 9. report, API, config

def test_morning_report_and_dashboard_status(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, phd2="Guiding")
    assert npil.status(cfg, NIGHT)["line"] == "Night pilot (act): no action tonight"
    assert npil.morning_lines(cfg, NIGHT) == []
    _tonight_bad_subs(cfg)
    _diag(cfg, "04:23", "guider_conflict", "stop_phd2", ecc=0.76)
    _tick(cfg, armer, io, "04:24")
    s = npil.status(cfg, NIGHT)
    assert s["line"].startswith("Night pilot (act): 22:24 stopped PHD2 guiding -> checking at ")
    assert s["last"]["action"] == "stop_phd2"
    _sub(cfg, "04:26", 0.5)
    _sub(cfg, "04:31", 0.5)
    _tick(cfg, armer, io, "04:37")
    s = npil.status(cfg, NIGHT)
    assert s["line"] == ("Night pilot (act): 22:24 stopped PHD2 guiding -> worked "
                         "(ecc 0.76 -> 0.5)")
    card = mr.report_card(cfg, NIGHT, projects=[], calibration=False,
                          completeness=False)
    assert card["night_pilot"][0] == ("Night pilot: 22:24 stopped PHD2 guiding -> "
                                      "worked (ecc 0.76 -> 0.5)")
    assert card["night_pilot"][0] in card["lines"]
    assert mr.card_line_priority(card["night_pilot"][0]) == mr.PRI_RIG
    assert any(x.startswith("Night pilot learned:") for x in card["lines"])
    assert npil.status(_cfg(tmp_path, night_pilot_mode="off"), NIGHT)["line"] == \
        "Night pilot: off"


def test_api_and_dashboard_wiring(tmp_path, monkeypatch):
    from photonscript.scheduler import app as app_mod
    from photonscript.scheduler.routers.night_pilot import api_night_pilot
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(app_mod, "get_config", lambda: cfg)
    monkeypatch.setattr(app_mod, "get_armer", lambda: FakeArmer())
    from fastapi.testclient import TestClient
    r = TestClient(app_mod.app).get("/api/night/pilot")   # no startup events
    assert r.status_code == 200 and r.json()["night"] == NIGHT
    out = api_night_pilot()
    assert out["mode"] == "act" and out["night"] == NIGHT and "line" in out
    html = (ROOT / "photonscript/scheduler/templates/dashboard.html").read_text(encoding="utf-8")
    assert 'id="nightPilotLine"' in html and "/api/night/pilot" in html
    src = (ROOT / "photonscript/scheduler/app.py").read_text(encoding="utf-8")
    assert "_spawn(run_pilot(get_config, get_armer))" in src


def test_config_fields_defaults():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.night_pilot_mode == "act"
    assert (c.night_pilot_stop_phd2, c.night_pilot_unguided_fallback,
            c.night_pilot_shorten_subs, c.night_pilot_af,
            c.night_pilot_skip_target) == (True, True, True, True, True)
    assert c.night_pilot_max_actions == 6 and c.night_pilot_rig_cooldown_min == 15.0
    assert c.night_pilot_sub_steps == "300,180,120" and c.night_pilot_sub_floor_s == 120.0
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    for attr in [a for a in PhotonScriptConfig.model_fields if a.startswith("night_pilot_")]:
        f = by_env["PS_" + attr.upper()]
        assert f[0] == attr and f[3] == "Night pilot"


def test_pilot_never_touches_roof_park_arm_or_calibration():
    src = (ROOT / "photonscript/scheduler/night_pilot.py").read_text(encoding="utf-8")
    assert src.isascii()
    for bad in ("mount_park", "park(", ".arm(", ".disarm(", "make_safe(",
                "set_calibration", "clear_calibration", "flip_calibration",
                "safetymonitor", "set_connected", "nina_warm", "dawn_shutdown(",
                "guider_start"):
        assert bad not in src, bad
