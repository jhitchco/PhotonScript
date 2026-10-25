"""Night pilot sub-length ladder (2026-10-25, scheduler/sub_ladder.py):
each rig finds its own longest good sub length per night.

  1. the pure decision: step up after N round accepted subs, wait, top rung;
  2. step down (M of the last W fail at a stepped-to rung) and hold;
  3. blocks: optics verdict, narrowband / few stars, sky-limited, darks;
  4. advise vs act (and night_pilot_mode=advise caps it), the story push;
  5. the Piggy-600: OSC override + companion re-dispatch, the unguided cap;
  6. persistence: the learned rung and the plan heads-up line; dawn revert;
  7. config fields.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import night_pilot as npil
from photonscript.scheduler import night_story as ns
from photonscript.scheduler import sub_ladder as sl
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import append_jsonl, read_jsonl

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
    kw.setdefault("night_pilot_watch", False)
    kw.setdefault("night_pilot_ladder_mode", "act")
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path),
                              image_watch_dir=str(tmp_path / "fits"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


class FakeArmer:
    def __init__(self, state="RUNNING", dawn=T("12:00")):
        self.state = state
        self.plan = {"night_of": NIGHT}
        self.dawn = dawn
        self.calls = []
        self.last_restart = None
        self.recipe = None
        self.detail = ""
        self.restart_result = {"ok": True, "redispatched": True}

    def _use_guiding(self):
        return False

    def _dawn(self):
        return self.dawn

    def _recipe_tonight(self):
        return self.recipe

    async def restart(self, when="after_exposure", now=None):
        self.calls.append(("restart", when))
        return dict(self.restart_result)


class FakeIO(npil.PilotIO):
    def __init__(self, cfg, darks=(120, 180, 240, 300, 450)):
        self.cfg = cfg
        self.running = ["Heart Nebula", "Smart Exposure"]
        self.calls = []
        self.pushes = []
        self.darks = {float(x) for x in darks}
        self.redispatch_ok = True
        self._store = ProjectStore(cfg)

    async def nina1_running(self, cfg):
        self.calls.append("nina1_running")
        return self.running, "Heart Nebula"

    async def phd2_state(self, cfg):
        return "Stopped"

    async def piggy_redispatch(self, cfg, armer):
        self.calls.append("piggy_redispatch")
        return self.redispatch_ok, "NINA #2 stopped after the current sub; companion re-dispatched"

    def ladder_dark_status(self, cfg, rig):
        return lambda s: ({"match": "matched", "text": f"{s:g} s darks in the library"}
                          if float(s) in self.darks else
                          {"match": "scaled", "text": f"no {s:g} s darks: scaled"})

    def store(self):
        return self._store

    def roof_closed_since(self, cfg, now):
        return None

    async def notify(self, cfg, msg, priority=0, kind="info", fact=None):
        self.pushes.append({"msg": msg, "kind": kind, "priority": priority, "fact": fact})


_N = [0]


def _sub(cfg, hm, *, exp, ecc=0.33, rig="piggyback", filt=None, stars=400,
         bg=2000.0, passed=True, hfr=2.5, target="Heart Nebula", write=True):
    _N[0] += 1
    st = T(hm) if isinstance(hm, str) else hm
    rec = {"rig": rig, "file": f"{rig}/Heart/LIGHT/sub_{_N[0]:04d}.fits",
           "target": target, "filter": filt or ("OSC" if rig == "piggyback" else "L"),
           "exp_s": float(exp), "start_utc": st.strftime("%Y-%m-%dT%H:%M:%SZ"),
           "end_utc": (st + timedelta(seconds=exp)).strftime("%Y-%m-%dT%H:%M:%SZ"),
           "ecc": ecc, "hfr": hfr, "stars": stars, "background": bg,
           "passed_qa": passed}
    if write:
        p = Path(cfg.data_dir) / "runs" / f"{NIGHT}_subs.jsonl"
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    return rec


def _run(cfg, start: str, n: int, exp: float, **kw) -> list[dict]:
    """n subs back to back from start."""
    t = T(start)
    out = []
    for _ in range(n):
        out.append(_sub(cfg, t, exp=exp, **kw))
        t += timedelta(seconds=exp + 10)
    return out


def _tick(cfg, armer, io, hm):
    async def run():
        r = await npil.tick(cfg, armer, io, now=T(hm))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return r
    return asyncio.run(run())


def _ladder_events(cfg, step=None):
    return [r for r in read_jsonl(events_path(cfg, NIGHT))
            if r.get("kind") == "night_pilot" and r.get("value") == "ladder"
            and (step is None or r.get("step") == step)]


def _darks_all(s):
    return {"match": "matched", "text": "darks"}


# ------------------------------------------------------------- 1. decision

def test_rungs_and_defaults(tmp_path):
    cfg = _cfg(tmp_path)
    assert sl.rungs(cfg, "rc16") == [120, 180, 240, 300]
    assert sl.rungs(cfg, "piggyback") == [120, 180, 240, 300, 450]
    assert sl.ecc_max(cfg, "piggyback") == 0.45 and sl.ecc_max(cfg, "rc16") == 0.5
    assert sl.next_rung(cfg, "piggyback", 180) == 240
    assert sl.next_rung(cfg, "piggyback", 150) == 180      # off-ladder length
    assert sl.next_rung(cfg, "rc16", 300) is None
    assert sl.next_rung(cfg, "rc16", 600) is None          # guided NB 600 s


def test_step_up_after_n_round_subs(tmp_path):
    cfg = _cfg(tmp_path)
    subs = _run(cfg, "03:00", 6, 180, write=False)
    d = sl.evaluate(cfg, "piggyback", subs, dark_status=_darks_all)
    assert d["kind"] == "up" and (d["from_s"], d["to_s"]) == (180, 240)
    assert d["n"] == 6 and d["ecc"] == 0.33 and d["proven_s"] == 180
    assert sl.step_text(cfg, d) == "Piggy stepped to 240 s (6 round subs at 180 s, ecc 0.33)"
    # five are not enough; one not accepted or not round breaks the run
    assert sl.evaluate(cfg, "piggyback", subs[:5], dark_status=_darks_all)["kind"] == "wait"
    bad = subs[:5] + [_sub(cfg, "04:00", exp=180, passed=False, write=False)]
    assert sl.evaluate(cfg, "piggyback", bad, dark_status=_darks_all)["kind"] == "wait"
    egg = subs[:5] + [_sub(cfg, "04:00", exp=180, ecc=0.47, write=False)]
    assert sl.evaluate(cfg, "piggyback", egg, dark_status=_darks_all)["kind"] == "wait"
    # the RC16 limit is its own (0.5): ecc 0.47 is round there
    rc = _run(cfg, "03:00", 6, 120, rig="rc16", ecc=0.47, write=False)
    d = sl.evaluate(cfg, "rc16", rc, dark_status=_darks_all)
    assert d["kind"] == "up" and d["to_s"] == 180
    # the top rung: nothing to do, the rung is still proven
    top = _run(cfg, "03:00", 6, 300, rig="rc16", write=False)
    d = sl.evaluate(cfg, "rc16", top, dark_status=_darks_all)
    assert d["kind"] == "none" and d["proven_s"] == 300


def test_subs_before_since_and_other_targets_do_not_count(tmp_path):
    cfg = _cfg(tmp_path)
    subs = _run(cfg, "03:00", 4, 180, write=False) + \
        _run(cfg, "03:50", 3, 180, target="M31", write=False)
    assert sl.evaluate(cfg, "piggyback", subs, dark_status=_darks_all)["kind"] == "wait"
    subs = _run(cfg, "03:00", 8, 180, write=False)
    d = sl.evaluate(cfg, "piggyback", subs, since=T("03:10"), dark_status=_darks_all)
    assert d["kind"] == "wait"


# ------------------------------------------------------------- 2. down + hold

def test_step_down_and_hold(tmp_path):
    cfg = _cfg(tmp_path)
    up = {"target": "Heart Nebula", "from_s": 180.0, "to_s": 240.0, "dir": "up"}
    subs = _run(cfg, "04:00", 2, 240, write=False) + \
        _run(cfg, "04:10", 3, 240, ecc=0.52, write=False)
    d = sl.evaluate(cfg, "piggyback", subs, last_up=up, dark_status=_darks_all)
    assert d["kind"] == "down" and (d["from_s"], d["to_s"]) == (240, 180)
    assert "3 of the last 5 subs at 240 s" in d["why"]
    # two fails of five: no step down (and no step up either: not 6 round)
    subs = _run(cfg, "04:00", 3, 240, write=False) + \
        _run(cfg, "04:15", 2, 240, ecc=0.52, write=False)
    assert sl.evaluate(cfg, "piggyback", subs, last_up=up,
                       dark_status=_darks_all)["kind"] == "wait"
    # held: round subs do not step up again
    subs = _run(cfg, "05:00", 6, 180, write=False)
    d = sl.evaluate(cfg, "piggyback", subs, held={"at_s": 180.0, "why": "x"},
                    dark_status=_darks_all)
    assert d["kind"] == "blocked" and "holding at 180 s" in d["why"]


def test_rc16_tick_steps_up_then_down_and_holds(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)
    armer, io = FakeArmer(), FakeIO(cfg)
    proj = io.store().add_from_target(HEART, budget_hours=5.0)
    io.store().update(proj.id, exposure_overrides={"Ha": 120.0})
    _run(cfg, "03:00", 6, 120, rig="rc16", filt="Ha", ecc=0.40, hfr=4.0)
    _tick(cfg, armer, io, "03:20")
    assert armer.calls == [("restart", "after_exposure")]
    st = npil.load_state(cfg, NIGHT)
    s = st["ladder"]["rc16"]["steps"][0]
    assert (s["dir"], s["from_s"], s["to_s"], s["status"]) == ("up", 120, 180, "done")
    assert io.store().projects[proj.id].exposure_overrides.get("Ha") == 180.0
    story = [p for p in io.pushes if p["kind"] == "story"]
    assert len(story) == 1 and "RC16 stepped to 180 s (6 round subs at 120 s, ecc 0.4)" \
        in story[0]["msg"]
    # 3 of the next 5 at 180 s fail: back to 120 s, held for the night
    _run(cfg, "03:25", 2, 180, rig="rc16", filt="Ha", ecc=0.42, hfr=4.0)
    _run(cfg, "03:32", 3, 180, rig="rc16", filt="Ha", ecc=0.58, hfr=4.0)
    _tick(cfg, armer, io, "03:50")
    st = npil.load_state(cfg, NIGHT)
    s = st["ladder"]["rc16"]["steps"][-1]
    assert (s["dir"], s["from_s"], s["to_s"]) == ("down", 180, 120)
    assert st["ladder"]["rc16"]["held"]["at_s"] == 120
    assert io.store().projects[proj.id].exposure_overrides.get("Ha") == 120.0
    assert len([p for p in io.pushes if p["kind"] == "story"]) == 2
    # round again at 120 s: holds, one blocked event
    _run(cfg, "03:55", 6, 120, rig="rc16", filt="Ha", ecc=0.40, hfr=4.0)
    _tick(cfg, armer, io, "04:20")
    _tick(cfg, armer, io, "04:21")
    assert len(armer.calls) == 2
    assert len([e for e in _ladder_events(cfg, "blocked") if "holding" in e["detail"]]) == 1
    learned = sl.learned_for(cfg, "rc16", "Heart Nebula")
    assert learned["rung_s"] == 120 and learned["failed_s"] == 180
    # a shorten right after a ladder step waits for it
    st = npil.load_state(cfg, NIGHT)
    d = {"rig": "rc16", "family": "tracking"}
    dec = {"action": "shorten_subs", "params": {}}
    why = npil.guard(cfg, armer, st, d, dec, {"running": []}, T("03:55"))
    assert why and "sub ladder changed" in why


# ------------------------------------------------------------- 3. blocks

def test_optics_verdict_blocks(tmp_path):
    cfg = _cfg(tmp_path, piggyback_enabled=False)
    subs = _run(cfg, "03:00", 6, 120, rig="rc16", write=False)
    d = sl.evaluate(cfg, "rc16", subs, optics=True, dark_status=_darks_all)
    assert d["kind"] == "blocked" and "optical" in d["why"]
    # through the tick: a sub doctor optics reading tonight (even low) holds it
    armer, io = FakeArmer(), FakeIO(cfg)
    io.store().add_from_target(HEART, budget_hours=5.0)
    append_jsonl(events_path(cfg, NIGHT), {
        "t": "2026-10-13T02:50:00Z", "rig": "rc16", "kind": "sub_doctor",
        "cause": "optics", "confidence": "low", "headline": "optics",
        "evidence": {}, "suggested_action": {"id": "check_optics"}})
    _tick(cfg, armer, io, "02:51")
    _run(cfg, "03:00", 6, 120, rig="rc16")
    _tick(cfg, armer, io, "03:20")
    assert armer.calls == []
    ev = _ladder_events(cfg, "blocked")
    assert ev and "optical" in ev[0]["detail"]


def test_narrowband_and_few_stars_block(tmp_path):
    cfg = _cfg(tmp_path)
    nb = _run(cfg, "03:00", 6, 120, rig="rc16", filt="Ha", stars=60, write=False)
    d = sl.evaluate(cfg, "rc16", nb, dark_status=_darks_all)
    assert d["kind"] == "blocked" and "narrowband" in d["why"]
    nb_ok = _run(cfg, "03:00", 6, 120, rig="rc16", filt="Ha", stars=250, write=False)
    assert sl.evaluate(cfg, "rc16", nb_ok, dark_status=_darks_all)["kind"] == "up"
    bb = _run(cfg, "03:00", 6, 120, rig="rc16", filt="L", stars=60, write=False)
    assert sl.evaluate(cfg, "rc16", bb, dark_status=_darks_all)["kind"] == "up"
    few = _run(cfg, "03:00", 6, 120, rig="rc16", filt="L", stars=12, write=False)
    d = sl.evaluate(cfg, "rc16", few, dark_status=_darks_all)
    assert d["kind"] == "blocked" and "12 stars" in d["why"]


def test_sky_limited_cap(tmp_path):
    cfg = _cfg(tmp_path)   # 0.25 x 65000 = 16250 ADU
    over_next = _run(cfg, "03:00", 6, 180, bg=15000, write=False)
    d = sl.evaluate(cfg, "piggyback", over_next, dark_status=_darks_all)
    assert d["kind"] == "blocked" and "sky-limited at 240 s" in d["why"]
    now = _run(cfg, "03:00", 6, 180, bg=17000, write=False)
    d = sl.evaluate(cfg, "piggyback", now, dark_status=_darks_all)
    assert d["kind"] == "blocked" and "at 180 s is over" in d["why"]
    dark_sky = _run(cfg, "03:00", 6, 180, bg=9000, write=False)
    assert sl.evaluate(cfg, "piggyback", dark_sky, dark_status=_darks_all)["kind"] == "up"
    cfg2 = _cfg(tmp_path, night_pilot_ladder_sky_frac=0.5)
    assert sl.evaluate(cfg2, "piggyback", over_next, dark_status=_darks_all)["kind"] == "up"


def test_darks_gate_logs_and_stays(tmp_path):
    cfg = _cfg(tmp_path)
    subs = _run(cfg, "03:00", 6, 180, write=False)
    for m in ("scaled", "none"):
        d = sl.evaluate(cfg, "piggyback", subs,
                        dark_status=lambda s, m=m: {"match": m, "text": "no darks"})
        assert d["kind"] == "blocked" and "no 240 s darks" in d["why"]
    armer, io = FakeArmer(), FakeIO(cfg, darks=(120, 180))
    io.store().add_from_target(HEART, budget_hours=5.0)
    _run(cfg, "03:00", 6, 180)
    _tick(cfg, armer, io, "03:30")
    _tick(cfg, armer, io, "03:31")
    assert "piggy_redispatch" not in io.calls and armer.calls == []
    ev = _ladder_events(cfg, "blocked")
    assert len(ev) == 1 and "no 240 s darks" in ev[0]["detail"]
    assert not [p for p in io.pushes if p["kind"] in ("story", "act")]


# ------------------------------------------------------------- 4. advise / act

def test_advise_records_would_step_and_changes_nothing(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_ladder_mode="advise")
    armer, io = FakeArmer(), FakeIO(cfg)
    proj = io.store().add_from_target(HEART, budget_hours=5.0)
    before = dict(io.store().projects[proj.id].exposure_overrides or {})
    _run(cfg, "03:00", 6, 180)
    _tick(cfg, armer, io, "03:30")
    _tick(cfg, armer, io, "03:31")
    assert "piggy_redispatch" not in io.calls and armer.calls == []
    assert dict(io.store().projects[proj.id].exposure_overrides or {}) == before
    st = npil.load_state(cfg, NIGHT)
    steps = st["ladder"]["piggyback"]["steps"]
    assert len(steps) == 1 and steps[0]["kind"] == "advise" and steps[0]["to_s"] == 240
    ev = _ladder_events(cfg, "would")
    assert len(ev) == 1 and "Piggy would step to 240 s on Heart Nebula" in ev[0]["detail"]
    assert [p["kind"] for p in io.pushes] == ["info"]      # storyline only
    assert any("would step 180 -> 240 s" in x for x in npil.morning_lines(cfg, NIGHT))
    # the proven rung is still learned in advise
    assert sl.learned_for(cfg, "piggyback", "Heart Nebula")["rung_s"] == 180


def test_pilot_mode_advise_caps_ladder_act(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_mode="advise", night_pilot_ladder_mode="act")
    assert sl.mode(cfg) == "advise"
    assert sl.mode(_cfg(tmp_path, night_pilot_mode="off")) == "off"
    assert sl.mode(_cfg(tmp_path, night_pilot_ladder_mode="off")) == "off"
    assert sl.mode(_cfg(tmp_path)) == "act"
    assert PhotonScriptConfig(_env_file=None).night_pilot_ladder_mode == "advise"


def test_act_held_by_live_guard(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    io.store().add_from_target(HEART, budget_hours=5.0)
    io.running = ["Heart Nebula", "Dusk flats OSC"]
    _run(cfg, "03:00", 6, 180)
    _tick(cfg, armer, io, "03:30")
    assert "piggy_redispatch" not in io.calls
    ev = _ladder_events(cfg, "blocked")
    assert ev and "held: NINA #1 runs" in ev[0]["detail"]


# ------------------------------------------------------------- 5. Piggy-600

def test_piggy_act_osc_override_and_companion(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    proj = io.store().add_from_target(HEART, budget_hours=5.0)
    armer.plan["piggy_length"] = {
        "base_s": 120.0, "cap_s": None, "targets": [
            {"target": "Heart Nebula", "exp_s": 180.0, "source": "override",
             "until_utc": "2026-10-13T08:00:00Z"},
            {"target": "M31", "exp_s": 120.0, "source": "default", "until_utc": None}],
        "segments": [], "single_s": None, "changed": True}
    _run(cfg, "03:00", 6, 180)
    _tick(cfg, armer, io, "03:30")
    _tick(cfg, armer, io, "03:31")
    assert io.calls.count("piggy_redispatch") == 1 and armer.calls == []
    ov = io.store().projects[proj.id].exposure_overrides
    assert ov.get("OSC") == 240.0
    segs = armer.plan["piggy_length"]["segments"]
    assert [s["exp_s"] for s in segs] == [240.0, 120.0]
    s = npil.load_state(cfg, NIGHT)["ladder"]["piggyback"]["steps"][0]
    assert s["status"] == "done" and s["effective_t"] == "2026-10-13T03:31:00Z"
    story = [p for p in io.pushes if p["kind"] == "story"]
    assert len(story) == 1
    assert story[0]["msg"].startswith(
        "Night pilot: Piggy stepped to 240 s (6 round subs at 180 s, ecc 0.33)")
    # dawn: the tonight-only override goes back
    npil.dawn_revert(cfg, NIGHT, store=io.store(), now=T("12:30"))
    assert "OSC" not in (io.store().projects[proj.id].exposure_overrides or {})


def test_piggy_unguided_cap_stops_the_step(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    proj = io.store().add_from_target(HEART, budget_hours=5.0)
    armer.plan["piggy_length"] = {"base_s": 120.0, "cap_s": 180.0, "targets": [
        {"target": "Heart Nebula", "exp_s": 180.0, "source": "default+unguided cap",
         "until_utc": None}], "segments": [], "single_s": 180.0, "changed": True}
    _run(cfg, "03:00", 6, 180)
    _tick(cfg, armer, io, "03:30")
    assert "piggy_redispatch" not in io.calls
    assert "OSC" not in (io.store().projects[proj.id].exposure_overrides or {})
    ev = _ladder_events(cfg, "failed")
    assert ev and "caps tonight at 180 s" in ev[0]["detail"]
    assert not [p for p in io.pushes if p["kind"] == "act"]


def test_piggy_redispatch_failure_pushes_act_once(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    io.store().add_from_target(HEART, budget_hours=5.0)
    io.redispatch_ok = False
    _run(cfg, "03:00", 6, 180)
    _tick(cfg, armer, io, "03:30")
    _tick(cfg, armer, io, "03:31")
    _tick(cfg, armer, io, "03:32")
    acts = [p for p in io.pushes if p["kind"] == "act"]
    assert len(acts) == 1 and "was not applied" in acts[0]["msg"]
    assert npil.load_state(cfg, NIGHT)["ladder"]["piggyback"]["held"]["at_s"] == 180


def test_with_target_length_rebuilds_segments():
    from photonscript.scheduler import piggy_length as pl
    rec, s = pl.with_target_length(None, "Heart Nebula", 240, 120)
    assert s == 240 and rec["single_s"] == 240 and rec["changed"]
    assert pl.companion_kwargs(rec) == {"exp_s": 240.0}


# ------------------------------------------------------------- 6. persistence

def test_learned_rung_persists_and_heads_up(tmp_path):
    cfg = _cfg(tmp_path)
    assert sl.record_best(cfg, NIGHT, "piggyback", "Heart Nebula", 180, ecc=0.33, n=6)
    assert sl.record_best(cfg, NIGHT, "piggyback", "Heart Nebula", 240, ecc=0.35, n=6)
    assert not sl.record_best(cfg, NIGHT, "piggyback", "Heart Nebula", 180, ecc=0.3, n=6)
    assert sl.learned_for(cfg, "piggyback", "heart nebula")["rung_s"] == 240
    # a new night replaces the old value
    assert sl.record_best(cfg, "2026-10-13", "piggyback", "Heart Nebula", 180, n=6)
    assert sl.learned_for(cfg, "piggyback", "Heart Nebula")["night"] == "2026-10-13"
    sl.record_best(cfg, "2026-10-13", "rc16", "Heart Nebula", 120, n=6, failed_s=180)
    raw = json.loads((Path(cfg.data_dir) / "night_pilot" / "ladder_learned.json")
                     .read_text(encoding="utf-8"))
    assert set(raw["rigs"]) == {"piggyback", "rc16"}
    plan = {"night_of": "2026-10-14", "schedule": [{"name": "Heart Nebula"},
                                                   {"name": "M31"}]}
    line = sl.headsup_line(cfg, plan)
    assert line.startswith("Sub ladder learned: Heart Nebula Piggy 180 s, RC16 120 s "
                           "(180 s failed)")
    assert "suggestion" in line
    assert sl.headsup_line(cfg, {"schedule": [{"name": "M31"}]}) is None
    text = ns.plan_headsup_text(cfg, {"night_of": "2026-10-14", "schedule": []},
                                guided=False, ladder=line)
    assert "Sub ladder learned" in text


def test_status_and_morning_lines(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    io.store().add_from_target(HEART, budget_hours=5.0)
    _run(cfg, "03:00", 6, 180)
    _tick(cfg, armer, io, "03:30")
    _tick(cfg, armer, io, "03:31")
    stat = npil.status(cfg, NIGHT, now=T("03:32"))
    assert stat["ladder"]["mode"] == "act"
    assert stat["ladder"]["rigs"]["piggyback"]["steps"][0]["to_s"] == 240
    lines = npil.morning_lines(cfg, NIGHT)
    assert any(x.startswith("Night pilot sub ladder Piggy:") and "180 -> 240 s" in x
               for x in lines)


# ------------------------------------------------------------- 7. config

def test_config_fields(tmp_path):
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig(_env_file=None)
    assert c.night_pilot_ladder_rc16 == "120,180,240,300"
    assert c.night_pilot_ladder_piggyback == "120,180,240,300,450"
    assert (c.night_pilot_ladder_up_subs, c.night_pilot_ladder_down_fails,
            c.night_pilot_ladder_down_window) == (6, 3, 5)
    assert (c.night_pilot_ladder_ecc_piggyback, c.night_pilot_ladder_ecc_rc16) == (0.45, 0.5)
    by_env = {f[1]: f for f in _CONFIG_FIELDS}
    for attr in [a for a in PhotonScriptConfig.model_fields
                 if a.startswith("night_pilot_ladder_")]:
        assert by_env["PS_" + attr.upper()][3] == "Night pilot"
    for f in ("photonscript/scheduler/sub_ladder.py", "photonscript/scheduler/night_pilot.py"):
        src = (Path(__file__).resolve().parents[2] / f).read_text(encoding="utf-8")
        assert src.isascii() and chr(0x2014) not in src
