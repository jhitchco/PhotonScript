"""Night pilot sub watch (2026-10-14): every graded sub, both rigs paired by
time, turned into pilot actions with their own modes (default advise), and
the closed-roof darks.

  1. sub_watch signals: focus drift (and the pairing / AF checks that make
     it seeing instead), trailing past the learned unguided limit (and the
     witness that makes it a human), PHD2 on an unguided night, clouds;
  2. the pilot: per-action modes (advise by default, act individually,
     night_pilot_mode caps), each action path with fakes (refocus -> AF,
     limit_subs -> sub cap + restart, guider_stop -> the unguided guard
     hook, else PHD2 stop), the immediate alert once per spell, a doctor
     diagnosis and a watch signal never both acting, advise rate limits;
  3. closed roof: the pilot's spell record and the companion's
     OSC_CLOSED_ROOF_DARKS (450 s first, roof + cooler gated, dawn bounded);
  4. the 2026-10-13 backtest from read-only fixtures;
  5. config, settings rows, morning report.
"""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

from photonscript.scheduler import night_pilot as npil
from photonscript.scheduler import pilot_backtest as pb
from photonscript.scheduler import pilot_hooks
from photonscript.scheduler import sub_watch as sw
from photonscript.scheduler.project_store import ProjectStore
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import CelestialTarget
from photonscript.shared.night_events import events_path
from photonscript.shared.phd2_store import read_jsonl

ROOT = Path(__file__).resolve().parents[2]
FIX = Path(__file__).parent / "fixtures" / "pilot_watch"
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
                              library_dir=str(tmp_path / "lib"),
                              nina_logs_dir=str(tmp_path / "logs"), **kw)


def rec(hm, rig="rc16", *, hfr=7.0, ecc=0.45, exp=180.0, flt="L", target="Andromeda Galaxy",
        guide="stopped", stars=400, ra=10.7, dec=41.3, n=[0]):
    """A graded light with a PS-21 scorecard (RC16 gates: ecc 0.6, HFR 10;
    Piggy-600: ecc 0.75, HFR 4.5)."""
    n[0] += 1
    st = T(hm)
    ecc_gate, hfr_gate = (0.6, 10.0) if rig == "rc16" else (0.75, 4.5)
    rows = [["ecc", ecc, ecc_gate, "fail" if ecc > ecc_gate else "pass"],
            ["hfr", hfr, hfr_gate, "fail" if hfr > hfr_gate else "pass"],
            ["stars", stars, [5, 5000], "pass" if stars >= 5 else "fail"],
            ["roof", "sky", "sky frame", "pass"]]
    return {"rig": rig, "file": f"{rig}/LIGHT/s_{n[0]:04d}.fits", "target": target,
            "filter": flt if rig == "rc16" else "OSC", "exp_s": exp,
            "start_utc": st.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "end_utc": (st + timedelta(seconds=exp)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "time": (st + timedelta(seconds=exp + 15)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "hfr": hfr, "ecc": ecc, "stars": stars, "guide_state": guide,
            "passed_qa": ecc <= ecc_gate and hfr <= hfr_gate,
            "scorecard": {"rows": rows},
            "pointing": {"mount_ra": ra * 15.0, "mount_dec": dec}}


def write(cfg, subs):
    p = Path(cfg.data_dir) / "runs" / f"{NIGHT}_subs.jsonl"
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        for r in subs:
            f.write(json.dumps(r) + "\n")


def sharp_then_soft(rig="rc16", soft=9.0, start="03:00", n_sharp=4, n_soft=3,
                    step=4, **kw):
    t0 = T(start)
    out = []
    for i in range(n_sharp + n_soft):
        hm = (t0 + timedelta(minutes=step * i)).strftime("%H:%M")
        out.append(rec(hm, rig, hfr=7.0 if i < n_sharp else soft, **kw))
    return out


def piggy_steady(start="03:00", n=7, step=4, hfr=3.0):
    t0 = T(start)
    return [rec((t0 + timedelta(minutes=step * i)).strftime("%H:%M"), "piggyback",
                hfr=hfr, ecc=0.5, exp=200.0) for i in range(n)]


CTX = {"guided": False, "phd2": None, "limits": {}, "af_finished": {}}


# ------------------------------------------------------------- 1. signals

def test_focus_drift_one_rig_is_refocus(tmp_path):
    cfg = _cfg(tmp_path)
    subs = sharp_then_soft() + piggy_steady()
    sigs, seen = sw.signals(cfg, subs, CTX)
    s = next(x for x in sigs if x["rig"] == "rc16")
    assert (s["cause"], s["action"], s["source"]) == ("focus_drift", "refocus", "watch")
    assert s["evidence"]["best_hfr"] == 7.0 and s["evidence"]["other_rig_sharp"]
    assert s["confidence"] == "high"
    assert "HFR 9.0 px vs the night's best 7.0 px" in s["headline"]
    # each sub is judged once: nothing new, no signal
    assert sw.signals(cfg, subs, CTX, seen)[0] == []


def test_focus_both_rigs_soft_is_the_sky(tmp_path):
    cfg = _cfg(tmp_path)
    pig = piggy_steady(n=4) + [rec(t, "piggyback", hfr=3.9, ecc=0.5, exp=200.0)
                               for t in ("03:16", "03:20", "03:24")]
    s = next(x for x in sw.signals(cfg, sharp_then_soft() + pig, CTX)[0]
             if x["rig"] == "rc16")
    assert s["cause"] == "seeing" and s["action"] == "none"
    assert "rose too" in s["headline"]


def test_focus_not_fixed_by_an_af_is_not_drift(tmp_path):
    cfg = _cfg(tmp_path)
    subs = sharp_then_soft(n_soft=4) + piggy_steady(n=8)
    # NINA's own AF finished after the softening began; the next sub as soft
    ctx = {**CTX, "af_finished": {"rc16": [T("03:17")]}}
    s = next(x for x in sw.signals(cfg, subs, ctx)[0] if x["rig"] == "rc16")
    assert s["cause"] == "seeing" and "did not bring it back" in s["headline"]
    # an AF before the drift began: it is drift
    ctx = {**CTX, "af_finished": {"rc16": [T("02:50")]}}
    s = next(x for x in sw.signals(cfg, subs, ctx)[0] if x["rig"] == "rc16")
    assert s["action"] == "refocus"
    # the AF history unknown: the pilot is asked to read it first
    s = next(x for x in sw.signals(cfg, subs, {**CTX, "af_finished": None})[0]
             if x["rig"] == "rc16")
    assert s["evidence"]["needs_af"] is True


def test_trailing_soft_stars_are_not_focus(tmp_path):
    cfg = _cfg(tmp_path)
    rc = sharp_then_soft()
    for r in rc[-3:]:
        r["scorecard"]["rows"][0][3] = "fail"     # trailing
        r["ecc"] = 0.7
    subs = rc + piggy_steady()
    sigs = [x for x in sw.signals(cfg, subs, CTX)[0] if x["rig"] == "rc16"]
    assert all(x["cause"] != "focus_drift" for x in sigs)


TABLE = {"rc16": {"ladder": [300.0, 240.0, 180.0, 120.0], "regions": [
    {"key": "dec+50..+70|ha-3..-1", "dec_band": [50, 70], "ha_band": [-3.0, -1.0],
     "label": "Dec +50..+70, east", "limit_s": 120.0, "source": "drift model",
     "confidence": "low"}]}}


def _heart_trailing(start="05:20", n=3, ecc=0.68, step=3, **kw):
    t0 = T(start)
    # Heart (RA 2.55 h) at 05:20Z from Rodeo: hour angle about -2 h (east)
    return [rec((t0 + timedelta(minutes=step * i)).strftime("%H:%M"), "rc16",
                hfr=10.0, ecc=ecc, flt="Ha", target="Heart Nebula", ra=2.55,
                dec=61.5, **kw) for i in range(n)]


def test_trailing_past_the_learned_limit_is_limit_subs(tmp_path):
    cfg = _cfg(tmp_path)
    ha = sw.region_limit(cfg, TABLE["rc16"], _heart_trailing()[-1])
    assert ha and ha["limit_s"] == 120.0 and ha["label"] == "Dec +50..+70, east"
    subs = _heart_trailing() + [rec("05:20", "piggyback", hfr=3.6, ecc=0.55,
                                    exp=360.0, target="Heart Nebula")]
    s = next(x for x in sw.signals(cfg, subs, {**CTX, "limits": TABLE})[0]
             if x["rig"] == "rc16")
    assert (s["cause"], s["action"]) == ("tracking_limit", "limit_subs")
    assert s["evidence"]["limit_s"] == 120.0 and s["evidence"]["exp_s"] == 180.0
    assert "round as a mount drift predicts" in s["evidence"]["witness"]
    # no table: noted, low confidence
    s = next(x for x in sw.signals(cfg, subs, CTX)[0] if x["rig"] == "rc16")
    assert s["action"] == "none" and s["confidence"] == "low"
    # guided night: no tracking signal
    s = [x for x in sw.signals(cfg, subs, {**CTX, "guided": True, "limits": TABLE})[0]
         if x["rig"] == "rc16"]
    assert all(x["action"] != "limit_subs" for x in s)


def test_only_one_rig_trailing_at_a_rate_the_other_would_show_is_a_human(tmp_path):
    cfg = _cfg(tmp_path)
    subs = _heart_trailing(ecc=0.85) + [rec("05:20", "piggyback", hfr=2.0, ecc=0.3,
                                            exp=360.0, target="Heart Nebula")]
    s = next(x for x in sw.signals(cfg, subs, {**CTX, "limits": TABLE})[0]
             if x["rig"] == "rc16")
    assert s["evidence"].get("human") is True and "only this rig" in s["headline"]


def test_guider_on_an_unguided_night(tmp_path):
    cfg = _cfg(tmp_path)
    subs = [rec("08:28", "rc16", ecc=0.9, guide="guiding"),
            rec("08:25", "piggyback", ecc=0.9, exp=300.0, guide="guiding")]
    sigs = sw.signals(cfg, subs, CTX)[0]
    assert {x["action"] for x in sigs} == {"guider_stop"}
    s = next(x for x in sigs if x["rig"] == "rc16")
    assert s["confidence"] == "high" and s["evidence"]["rigs_guiding"] == ["piggyback", "rc16"]
    assert sw.signals(cfg, subs, {**CTX, "guided": True})[0] == [] or all(
        x["action"] != "guider_stop" for x in sw.signals(cfg, subs, {**CTX, "guided": True})[0])
    # live PHD2 active with a sub that says stopped
    s = sw.signals(cfg, [rec("08:28", "rc16")], {**CTX, "phd2": "Guiding"})[0]
    assert s and s[0]["action"] == "guider_stop"


def test_clouds_on_both_rigs_is_a_note(tmp_path):
    cfg = _cfg(tmp_path)
    subs = [rec("04:00", "rc16", stars=2), rec("03:59", "piggyback", stars=2, exp=200.0)]
    s = next(x for x in sw.signals(cfg, subs, CTX)[0] if x["rig"] == "rc16")
    assert s["cause"] == "clouds" and s["action"] == "none"


# ------------------------------------------------------------- 2. the pilot

class FakeArmer:
    def __init__(self, state="RUNNING", guided=False, dawn=T("12:00")):
        self.state = state
        self.plan = {"night_of": NIGHT}
        self.guided = guided
        self.dawn = dawn
        self.calls = []
        self.last_restart = None
        self.detail = ""

    def _use_guiding(self):
        return self.guided

    def _dawn(self):
        return self.dawn

    def _recipe_tonight(self):
        return None

    async def restart(self, when="after_exposure", now=None):
        self.calls.append(("restart", when))
        return {"ok": True, "pending": True}

    async def fallback_unguided(self, reason):
        self.calls.append(("fallback_unguided", reason))
        return True

    async def _dispatch_piggyback_companion(self):
        self.calls.append(("companion",))


class FakeIO(npil.PilotIO):
    def __init__(self, cfg, target="Andromeda Galaxy", phd2=None, roof=None,
                 owed=None, limits=None, af=None):
        self.cfg = cfg
        self.target = target
        self.phd2 = phd2
        self.calls = []
        self.pushes = []
        self.roof = roof
        self.owed = owed or {}
        self.limits = limits or {}
        self.af = af if af is not None else {}
        self._store = ProjectStore(cfg)

    async def nina1_running(self, cfg):
        return [self.target, "L filter block (cooler-gated)", "Smart Exposure"], self.target

    async def phd2_state(self, cfg):
        return self.phd2

    async def phd2_stop(self, cfg):
        self.calls.append("phd2_stop")
        return True, "PHD2 now Stopped"

    async def nina1_wait_idle(self, cfg, max_s=300.0):
        return True

    async def nina1_autofocus(self, cfg):
        self.calls.append("nina1_autofocus")
        return True, "AUTOFOCUS-FINISHED, focuser 5200 -> 5260"

    async def piggy_af(self, cfg, armer):
        self.calls.append("piggy_af")
        return True, "companion re-dispatched"

    def store(self):
        return self._store

    async def notify(self, cfg, msg, priority=0, **_kw):
        self.pushes.append((priority, msg))

    def limits_tables(self, cfg):
        return self.limits

    async def af_finished(self, cfg):
        self.calls.append("af_finished")
        return self.af

    def roof_closed_since(self, cfg, now):
        return self.roof

    def darks_owed(self, cfg):
        return self.owed

    def light_lengths(self, cfg, armer):
        return {"rc16": [180.0], "piggyback": [450.0]}

    def dark_status(self, cfg):
        return lambda s: {"match": "matched", "text": f"darks match: {s:g} s"}


def _tick(cfg, armer, io, hm):
    return asyncio.run(npil.tick(cfg, armer, io, now=T(hm)))


def _ev(cfg, value=None):
    return [r for r in read_jsonl(events_path(cfg, NIGHT))
            if r.get("kind") == "night_pilot" and (value is None or r.get("value") == value)]


def test_watch_actions_default_advise_send_nothing(tmp_path):
    cfg = _cfg(tmp_path)          # night_pilot_mode act (code default)
    assert npil.action_mode(cfg, "refocus") == "advise"
    armer, io = FakeArmer(), FakeIO(cfg)
    write(cfg, sharp_then_soft() + piggy_steady())
    _tick(cfg, armer, io, "03:40")
    assert "nina1_autofocus" not in io.calls and armer.calls == []
    assert "af_finished" in io.calls            # read only when a focus signal needs it
    msg = io.pushes[-1][1]
    assert msg.startswith("Night pilot (advise): would have refocused the RC16 (HFR drift)")
    assert "night_pilot_refocus_mode=advise" in msg
    a = npil.load_state(cfg, NIGHT)["actions"][-1]
    assert a["kind"] == "advise" and a["source"] == "watch" and a["action"] == "refocus"


def test_refocus_act_runs_the_direct_af_and_verifies_on_hfr(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_refocus_mode="act")
    armer, io = FakeArmer(), FakeIO(cfg)
    write(cfg, sharp_then_soft() + piggy_steady())

    async def go():
        await npil.tick(cfg, armer, io, now=T("03:40"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        await npil.tick(cfg, armer, io, now=T("03:41"))
    asyncio.run(go())
    assert io.calls.count("nina1_autofocus") == 1
    a = next(x for x in npil.load_state(cfg, NIGHT)["actions"] if x["kind"] == "act")
    assert a["action"] == "refocus" and a["status"] == "done"
    assert a["baseline"]["metric"] == "hfr"
    write(cfg, [rec("03:45", hfr=7.1), rec("03:49", hfr=7.0)])
    _tick(cfg, armer, io, "03:55")
    a = next(x for x in npil.load_state(cfg, NIGHT)["actions"] if x["kind"] == "act")
    assert a["result"] == "improved"


def test_night_pilot_mode_advise_caps_an_act_action(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_mode="advise", night_pilot_refocus_mode="act")
    armer, io = FakeArmer(), FakeIO(cfg)
    write(cfg, sharp_then_soft() + piggy_steady())
    _tick(cfg, armer, io, "03:40")
    assert "nina1_autofocus" not in io.calls
    assert "night_pilot_mode=advise" in io.pushes[-1][1]


def test_action_mode_off_is_a_note_only(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_refocus_mode="off")
    armer, io = FakeArmer(), FakeIO(cfg)
    write(cfg, sharp_then_soft() + piggy_steady())
    _tick(cfg, armer, io, "03:40")
    assert io.pushes == [] and not npil.load_state(cfg, NIGHT)["actions"]
    assert any("night_pilot_refocus_mode=off" in e["detail"] for e in _ev(cfg, "annotate"))


def test_limit_subs_act_caps_the_goal_and_restarts(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_limit_subs_mode="act")
    armer, io = FakeArmer(), FakeIO(cfg, target="Heart Nebula", limits=TABLE)
    proj = io.store().add_from_target(HEART, budget_hours=5.0)
    write(cfg, _heart_trailing() + [rec("05:18", "piggyback", hfr=3.6, ecc=0.55,
                                        exp=450.0, target="Heart Nebula")])
    _tick(cfg, armer, io, "05:35")
    assert armer.calls == [("restart", "after_exposure")]
    ov = io.store().projects[proj.id].exposure_overrides
    assert ov and set(ov.values()) == {120.0}
    a = next(x for x in npil.load_state(cfg, NIGHT)["actions"] if x["kind"] == "act")
    assert a["action"] == "limit_subs" and a["params"]["to_s"] == 120.0
    assert "learned unguided limit, Dec +50..+70, east" in a["what"]
    # restored at dawn
    txt = npil.dawn_revert(cfg, NIGHT, store=io.store())
    assert txt.startswith("night pilot restored Heart Nebula")


def test_doctor_and_watch_never_both_shorten(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_limit_subs_mode="act")
    armer, io = FakeArmer(), FakeIO(cfg, target="Heart Nebula", limits=TABLE)
    io.store().add_from_target(HEART, budget_hours=5.0)
    write(cfg, _heart_trailing())
    _tick(cfg, armer, io, "05:35")           # the watch cuts 180 -> 120 s
    from photonscript.shared.phd2_store import append_jsonl
    append_jsonl(events_path(cfg, NIGHT), {
        "t": "2026-10-13T05:36:00Z", "rig": "rc16", "kind": "sub_doctor",
        "cause": "tracking", "confidence": "high", "headline": "rc16 tracking",
        "evidence": {"ecc": 0.68, "exp_s": 180, "shared_mount": True},
        "suggested_action": {"id": "shorten_subs"}})
    _tick(cfg, armer, io, "05:37")
    acts = [x for x in npil.load_state(cfg, NIGHT)["actions"] if x["kind"] == "act"]
    assert [x["action"] for x in acts] == ["limit_subs"]
    assert any("waiting for the limit_subs check" in e["detail"] for e in _ev(cfg, "held"))


def test_guider_stop_alert_once_per_spell_and_hook(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg)
    write(cfg, [rec("08:28", ecc=0.9, guide="guiding")])
    _tick(cfg, armer, io, "08:32")
    alerts = [m for p, m in io.pushes if p == 1]
    assert len(alerts) == 1 and alerts[0].startswith("Night pilot ALERT: PHD2 guiding on an UNGUIDED")
    assert "phd2_stop" not in io.calls                    # advise by default
    write(cfg, [rec("08:31", ecc=0.9, guide="guiding")])
    _tick(cfg, armer, io, "08:35")
    assert len([1 for p, _m in io.pushes if p == 1]) == 1  # same spell
    write(cfg, [rec("08:34", guide="stopped")])
    _tick(cfg, armer, io, "08:38")
    write(cfg, [rec("08:57", ecc=0.9, guide="guiding")])
    _tick(cfg, armer, io, "09:01")
    assert len([1 for p, _m in io.pushes if p == 1]) == 2  # a new spell

    # act: the unguided guard's hook first ...
    cfg2 = _cfg(tmp_path / "b", night_pilot_guider_stop_mode="act")
    io2 = FakeIO(cfg2)
    seen = []

    async def hook(cfg, rig, evidence, now):
        seen.append((rig, evidence["because"]))
        return {"ok": True, "detail": "guard stopped guiding"}
    monkeypatch.setattr(pilot_hooks, "guider_active_unguided", hook)
    write(cfg2, [rec("08:28", ecc=0.9, guide="guiding")])
    _tick(cfg2, armer, io2, "08:32")
    assert seen and "phd2_stop" not in io2.calls
    a = next(x for x in npil.load_state(cfg2, NIGHT)["actions"] if x["kind"] == "act")
    assert a["status"] == "done" and a.get("hook") is True
    # ... and PHD2 stop_capture when the hook does not handle it
    monkeypatch.setattr(pilot_hooks, "guider_active_unguided", lambda *a, **k: None)
    cfg3 = _cfg(tmp_path / "c", night_pilot_guider_stop_mode="act")
    io3 = FakeIO(cfg3)
    write(cfg3, [rec("08:28", ecc=0.9, guide="guiding")])
    _tick(cfg3, armer, io3, "08:32")
    assert io3.calls.count("phd2_stop") == 1


def test_default_hook_defers_to_guard_default_policy():
    # the guard's default policy is disconnect, so the pilot leaves it there
    out = pilot_hooks.guider_active_unguided(None, "rc16", {}, None)
    assert out and out["ok"]


def test_watch_never_acts_unless_running(tmp_path):
    cfg = _cfg(tmp_path, night_pilot_guider_stop_mode="act")
    io = FakeIO(cfg)
    write(cfg, [rec("08:28", ecc=0.9, guide="guiding")])
    _tick(cfg, FakeArmer(state="WATCHING"), io, "08:32")
    assert io.calls.count("phd2_stop") == 0 and io.pushes == []


def test_advise_timeline_is_rate_limited_not_flapping(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, target="Heart Nebula", limits=TABLE)
    write(cfg, _heart_trailing(n=3))
    _tick(cfg, armer, io, "05:35")
    for k in range(6):                      # the subs keep trailing at 180 s
        t = T("05:30") + timedelta(minutes=3 * k)
        write(cfg, _heart_trailing(start=t.strftime("%H:%M"), n=1))
        _tick(cfg, armer, io, (t + timedelta(minutes=4)).strftime("%H:%M"))
    adv = [a for a in npil.load_state(cfg, NIGHT)["actions"] if a["kind"] == "advise"]
    assert len(adv) == 1          # the same cut on the same target: advised once
    assert len([m for _p, m in io.pushes if "would have" in m]) == 1


# ------------------------------------------------------------- 3. closed roof

OWED = {"piggyback": [{"exp_s": 120.0, "owed": 0}, {"exp_s": 450.0, "owed": 22}],
        "rc16": [{"exp_s": 120.0, "owed": 30}, {"exp_s": 240.0, "owed": 30}]}


def test_closed_roof_plan():
    plan = sw.closed_roof_plan(OWED, 99, light_lengths={"piggyback": [450.0]})
    assert plan[0] == {"rig": "piggyback", "exp_s": 450.0, "owed": 22, "n": 13}
    assert [(p["rig"], p["exp_s"]) for p in plan[1:]] == [("rc16", 120.0), ("rc16", 240.0)]
    assert sw.closed_roof_plan(OWED, 25) == []


def test_closed_roof_spell_is_recorded_once(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = FakeArmer(), FakeIO(cfg, roof=T("10:20"), owed=OWED)
    _tick(cfg, armer, io, "10:21")
    _tick(cfg, armer, io, "10:22")
    st = npil.load_state(cfg, NIGHT)
    assert len(st["closed_roof"]) == 1
    txt = st["closed_roof"][0]["text"]
    assert "roof closed at 04:20 with 99 min to dawn" in txt
    assert "Piggy-600 450 s x13 (owed 22)" in txt
    assert len(io.pushes) == 1 and armer.calls == []
    assert any(x.startswith("Night pilot closed roof: roof closed at 04:20")
               for x in npil.morning_lines(cfg, NIGHT))
    # under the minimum to dawn: recorded, no darks
    cfg2 = _cfg(tmp_path / "b")
    io2 = FakeIO(cfg2, roof=T("11:40"), owed=OWED)
    _tick(cfg2, FakeArmer(), io2, "11:41")
    assert "no closed-roof darks" in npil.load_state(cfg2, NIGHT)["closed_roof"][0]["text"]
    # off: nothing
    cfg3 = _cfg(tmp_path / "c", closed_roof_darks="off")
    io3 = FakeIO(cfg3, roof=T("10:20"), owed=OWED)
    _tick(cfg3, FakeArmer(), io3, "10:21")
    assert not npil.load_state(cfg3, NIGHT).get("closed_roof")



# PS-228: the closed-roof line once per closure, local clock, no daytime spam

class MovingRoofIO(FakeIO):
    """The pre-fix shape: a window clipped to now - 14 h, so `since` moves
    forward every tick (10-18: 05:52 local read as "closed at 15:52")."""
    def roof_closed_since(self, cfg, now):
        return now - timedelta(hours=14)


def _night_armer(dusk="01:30", dawn="12:00"):
    a = FakeArmer(dawn=T(dawn))
    a.plan.update(dusk_utc=T(dusk).isoformat() + "Z", dawn_utc=T(dawn).isoformat() + "Z")
    return a


def test_ps228_closed_all_day_is_one_record_no_push(tmp_path):
    """10-18 / 10-21 shape: the roof closed since the previous afternoon,
    one tick a minute until dawn: one record, no push, no clipped clock."""
    cfg = _cfg(tmp_path)
    armer, io = _night_armer(dawn="12:00"), MovingRoofIO(cfg, owed=OWED)
    for hm in ("11:44", "11:45", "11:46", "11:47", "11:52", "11:58"):
        _tick(cfg, armer, io, hm)
    st = npil.load_state(cfg, NIGHT)
    assert len(st["closed_roof"]) == 1
    txt = st["closed_roof"][0]["text"]
    assert txt.startswith("roof closed since dusk (19:30)") and "15:" not in txt
    assert io.pushes == []
    assert len(_ev(cfg, "closed_roof")) == 1
    # after dawn (and the next day before dusk) nothing at all
    for hm in ("12:00", "12:01", "18:00"):
        _tick(cfg, armer, io, hm)
    assert len(npil.load_state(cfg, NIGHT)["closed_roof"]) == 1 and io.pushes == []


def test_ps228_mid_night_closure_one_record_one_push(tmp_path):
    """10-14 shape: closed mid-night with time left: one record, one push,
    the closure time in local time; reopening and closing again is a new
    spell."""
    cfg = _cfg(tmp_path)
    armer, io = _night_armer(), FakeIO(cfg, roof=T("06:03"), owed=OWED)
    for hm in ("06:04", "06:05", "06:06"):
        _tick(cfg, armer, io, hm)
    st = npil.load_state(cfg, NIGHT)
    assert len(st["closed_roof"]) == 1 and len(io.pushes) == 1
    assert "roof closed at 00:03 with 356 min to dawn" in io.pushes[0][1]
    io.roof = None
    _tick(cfg, armer, io, "07:00")
    io.roof = T("08:00")
    _tick(cfg, armer, io, "08:01")
    _tick(cfg, armer, io, "08:02")
    assert len(npil.load_state(cfg, NIGHT)["closed_roof"]) == 2 and len(io.pushes) == 2


def test_ps228_under_min_left_is_recorded_not_pushed(tmp_path):
    cfg = _cfg(tmp_path)
    armer, io = _night_armer(), FakeIO(cfg, roof=T("11:40"), owed=OWED)
    _tick(cfg, armer, io, "11:41")
    _tick(cfg, armer, io, "11:42")
    st = npil.load_state(cfg, NIGHT)
    assert len(st["closed_roof"]) == 1 and "no closed-roof darks" in st["closed_roof"][0]["text"]
    assert io.pushes == []


def test_ps228_roof_closed_since_is_not_clipped_at_14h(tmp_path):
    """The real PilotIO: a roof closed 20 h ago reads its true start."""
    cfg = _cfg(tmp_path)
    now = T("11:52")
    start = now - timedelta(hours=20)
    (tmp_path / "safety_history.jsonl").write_text(
        json.dumps({"ts": (start - timedelta(hours=3)).isoformat() + "Z", "state": "safe"}) + "\n"
        + json.dumps({"ts": start.isoformat() + "Z", "state": "unsafe"}) + "\n",
        encoding="utf-8")
    assert npil.PilotIO().roof_closed_since(cfg, now) == start
    assert npil.PilotIO().roof_closed_since(cfg, now + timedelta(minutes=1)) == start

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _companion(tmp_path, **kw):
    from photonscript.scheduler.calibration import generate_piggyback_companion_json
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    kw.setdefault("piggyback_dark_exposures", "120,450")
    kw.setdefault("piggyback_exposure_s", 450.0)
    cfg = _cfg(tmp_path, **kw)
    return json.loads(generate_piggyback_companion_json(
        rig_config(cfg, PIGGYBACK), has_safety=True, with_lights=True))


def _named(root, name):
    return [n for n in _walk(root) if n.get("Name") == name]


def test_companion_closed_roof_darks_450_first_gated_and_dawn_bounded(tmp_path):
    root = _companion(tmp_path)
    loop = _named(root, "OSC_LIGHTS_UNTIL_DAWN")[0]
    items = loop["Items"]["$values"] if isinstance(loop["Items"], dict) else loop["Items"]
    names = [i.get("Name") for i in items]
    crd_i = names.index("OSC_CLOSED_ROOF_DARKS")
    assert crd_i < names.index("WAIT_SAFE_OR_ASTRO_DAWN")
    crd = items[crd_i]
    conds = json.dumps(crd.get("Conditions"))
    assert "LoopWhileUnsafe" in conds and "TimeCondition" in conds
    darks = [n for n in _walk(crd) if n.get("ImageType") == "DARK"]
    assert [d["ExposureTime"] for d in darks] == [450.0, 120.0]
    assert not [n for n in _walk(crd) if n.get("ImageType") == "LIGHT"]
    txt = json.dumps(crd)
    for word in ("Slew", "Park", "Guiding", "Center", "FilterWheel", "OpenCover"):
        assert word not in txt, word
    # each block roof-gated and ending one exposure before astro dawn
    blk = [n for n in _walk(crd) if str(n.get("Name") or "").startswith("OSC DARKS_450s")][0]
    assert "LoopWhileUnsafe" in json.dumps(blk.get("Conditions"))
    assert '"MinutesOffset": -8' in json.dumps(blk.get("Conditions")) or \
        "-8" in json.dumps(blk.get("Conditions"))


def test_companion_closed_roof_darks_off_and_manual(tmp_path):
    assert not _named(_companion(tmp_path, closed_roof_darks="off"), "OSC_CLOSED_ROOF_DARKS")
    assert not _named(_companion(tmp_path / "m", piggyback_dark_capture_window="manual"),
                      "OSC_CLOSED_ROOF_DARKS")


# ------------------------------------------------------------- 4. backtest

def test_backtest_2026_10_13(tmp_path):
    """Read-only fixtures (GETs of the scope PC on 2026-10-14): what the pilot
    would have done on the night of 2026-10-13."""
    cfg = _cfg(tmp_path)
    doc = pb.load_doc(FIX / "2026-10-13.json")
    rep = pb.run(cfg, doc, data_dir=str(tmp_path / "bt"))
    would = rep["would_have"]
    # M31 (Dec +41, east) and the Heart (Dec +61, east) trail at 180 s:
    # cut to the learned 120 s, once each
    assert would[0].startswith("21:48 cut RC16 subs 180 -> 120 s") and "Dec +30..+50, east" in would[0]
    assert would[1].startswith("23:31 cut RC16 subs 180 -> 120 s") and "Dec +50..+70, east" in would[1]
    # 02:28 flip: NINA started PHD2 guiding on the unguided night
    assert would[2].startswith("02:31 stopped PHD2 guiding")
    assert would[3].startswith("03:03 stopped PHD2 guiding")   # restarted after an AF
    assert len(would) == 4
    kinds = [(x["local"], x["kind"]) for x in rep["timeline"]]
    assert ("02:31", "alert") in kinds and ("03:00", "alert") in kinds
    # the Piggy-600's soft spell was the sky (the RC16 rose too): no refocus
    assert any(x["kind"] == "annotate" and "the sky, not focus" in x["detail"]
               for x in rep["timeline"])
    assert not any("refocus" in w for w in would)
    # 04:20 roof closed, 99 min to dawn: 13 of the 22 owed 450 s Piggy darks fit
    cr = rep["closed_roof"][0]
    assert cr["since"] == "2026-10-14T10:20:35Z" and cr["dark_left_min"] == 99
    assert cr["plan"][0] == {"rig": "piggyback", "exp_s": 450.0, "owed": 22, "n": 13}
    assert any(x.startswith("Night pilot would have: 21:48") for x in rep["morning"])
    # nothing was ever sent
    assert all("ALERT" in m or "would have" in m or "roof closed" in m
               or "needs a human" in m for _p, m in rep["pushes"])
    # without the (hindsight) limits table: the trailing is only noted
    rep2 = pb.run(cfg, doc, use_limits=False, data_dir=str(tmp_path / "bt2"))
    assert [w[:5] for w in rep2["would_have"]] == ["02:31", "03:03"]


# ------------------------------------------------------------- 5. config

def test_config_defaults_and_settings_rows():
    c = PhotonScriptConfig(_env_file=None)
    assert c.night_pilot_watch is True and c.night_pilot_watch_subs == 3
    assert c.night_pilot_watch_hfr_rise == 0.20
    assert (c.night_pilot_refocus_mode, c.night_pilot_limit_subs_mode,
            c.night_pilot_guider_stop_mode) == ("advise", "advise", "advise")
    assert c.closed_roof_darks == "on" and c.closed_roof_darks_min_left_min == 30.0
    src = (ROOT / "photonscript/scheduler/app.py").read_text(encoding="utf-8")
    for k in ("PS_NIGHT_PILOT_WATCH", "PS_NIGHT_PILOT_REFOCUS_MODE",
              "PS_NIGHT_PILOT_LIMIT_SUBS_MODE", "PS_NIGHT_PILOT_GUIDER_STOP_MODE",
              "PS_CLOSED_ROOF_DARKS", "PS_CLOSED_ROOF_DARKS_MIN_LEFT_MIN"):
        assert f'"{k}"' in src, k
    hb = (ROOT / "docs/HANDBOOK.md").read_text(encoding="utf-8")
    assert "night_pilot_refocus_mode" in hb and "OSC_CLOSED_ROOF_DARKS" in hb


def test_pilot_hook_defers_to_unguided_guard_on_disconnect():
    from types import SimpleNamespace
    from photonscript.scheduler import pilot_hooks
    out = pilot_hooks.guider_active_unguided(
        SimpleNamespace(unguided_guider_policy="disconnect"), "rc16", {})
    assert out and out["ok"] and "unguided guard" in out["detail"]
    for pol in ("alert", "off"):
        assert pilot_hooks.guider_active_unguided(
            SimpleNamespace(unguided_guider_policy=pol), "rc16", {}) is None
