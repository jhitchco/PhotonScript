"""PS-212: Pushover v2, the night storyline (shared/pushover.py kinds,
scheduler/night_story.py, GET /api/night/story, GET /api/pushover/weekly).

Kinds (act / story / info), the midnight cutoff, the plan heads-up builder
and its folding of arm-time acts, "imaging now", first light and target
switches, the debounced roof, the end-of-night report, watcher suppression,
the story and weekly endpoints, NINA's own push items, and a replay of the
2026-10-16..23 audit log through the policy.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import night_story as ns
from photonscript.shared import pushover as po
from photonscript.shared.config import PhotonScriptConfig


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def sent(monkeypatch):
    out: list[dict] = []

    async def fake(config, message, title, priority, sound, **kw):
        out.append({"title": title, "message": message, "priority": priority, **kw})
        return True
    monkeypatch.setattr(po, "_send_raw", fake)
    monkeypatch.setattr(po, "_is_daytime", lambda c, when=None: False)
    return out


def _cfg(tmp_path, **kw):
    return PhotonScriptConfig(_env_file=None, data_dir=str(tmp_path), **kw)


def _story(cfg, now=None):
    return ns.entries(cfg, po.night_of(cfg, now))


# ---------------------------------------------------------------------------
# kinds
# ---------------------------------------------------------------------------

def test_no_kind_is_info_logged_to_the_story_never_pushed(tmp_path, sent):
    cfg = _cfg(tmp_path)
    assert run(po.notify(cfg, "Nanny alive. 3 subs", title="PhotonScript heartbeat")) is False
    assert run(po.notify(cfg, "frozen EAF", title="PhotonScript frozen sensor",
                         kind="info", priority=1)) is False
    assert sent == []
    st = _story(cfg)
    assert [e["kind"] for e in st] == ["info", "info"]
    assert all(e["sent"] is False and e["reason"] == "info" for e in st)
    audit = [json.loads(x) for x in po._audit_path(cfg).read_text().splitlines()]
    assert [a["kind"] for a in audit] == ["info", "info"]


def test_story_pushed_once_per_fact_per_night(tmp_path, sent):
    cfg = _cfg(tmp_path)
    run(po.notify(cfg, "TPoint run finished: 99 points", title="PhotonScript TPoint run finished",
                  kind="story"))
    run(po.notify(cfg, "TPoint run finished: 26 points", title="PhotonScript TPoint run finished",
                  kind="story"))   # same fact, digits masked
    run(po.notify(cfg, "Roof reopened 22:10", title="PhotonScript roof", kind="story",
                  fact="roof:1"))
    run(po.notify(cfg, "Roof reopened 01:40", title="PhotonScript roof", kind="story",
                  fact="roof:2"))
    assert [s["title"] for s in sent] == ["PhotonScript TPoint run finished",
                                          "PhotonScript roof", "PhotonScript roof"]
    assert all(s["priority"] == 0 for s in sent)
    assert [e["reason"] for e in _story(cfg)] == ["sent", "dup-fact", "sent", "sent"]


def test_act_is_priority_one_with_upside_and_downside_once_per_title(tmp_path, sent):
    cfg = _cfg(tmp_path)
    run(po.notify(cfg, "Armed UNGUIDED but ProTrack is OFF in TheSky.",
                  title="PhotonScript ProTrack", kind="act", priority=0))
    run(po.notify(cfg, "ProTrack still OFF at the recheck.",
                  title="PhotonScript ProTrack", kind="act"))
    assert len(sent) == 1
    assert sent[0]["priority"] == 1
    assert "If you act: tick Activate ProTrack" in sent[0]["message"]
    assert "If not: unguided subs trail" in sent[0]["message"]
    assert _story(cfg)[1]["reason"] == "dup-fact"


def test_act_text_is_kept_when_the_caller_wrote_it():
    msg = po.act_text("Piggy-600 cooler stalled after 2 reconnects",
                      "a camera power cycle saves ~7 h of Piggy-600 data",
                      "the RC16 still images and the Piggy sits out")
    assert msg == ("Piggy-600 cooler stalled after 2 reconnects. If you act: a camera "
                   "power cycle saves ~7 h of Piggy-600 data. If not: the RC16 still "
                   "images and the Piggy sits out.")
    assert po.with_consequence("PhotonScript cooler recovery", msg) == msg


def test_kinds_setting_turns_a_kind_off_but_never_a_safety_act(tmp_path, sent):
    cfg = _cfg(tmp_path, pushover_kinds="act")
    assert run(po.notify(cfg, "First light", title="PhotonScript first light", kind="story")) is False
    assert _story(cfg)[-1]["reason"] == "story-off"
    cfg2 = _cfg(tmp_path / "b", pushover_kinds="")
    assert run(po.notify(cfg2, "ProTrack off", title="PhotonScript ProTrack", kind="act")) is False
    assert run(po.notify(cfg2, "Dispatch FAILED: lint", title="PhotonScript ERROR",
                         kind="act", safety=True)) is True
    assert [s["title"] for s in sent] == ["PhotonScript ERROR"]


def test_enabled_kinds_parses_and_never_enables_info():
    assert po.enabled_kinds(type("C", (), {"pushover_kinds": "act, story,info"})()) == {"act", "story"}
    assert po.enabled_kinds(type("C", (), {"pushover_kinds": ["story"]})()) == {"story"}
    assert po.norm_kind("ACT") == "act" and po.norm_kind("loud") == "info"


# ---------------------------------------------------------------------------
# the midnight cutoff
# ---------------------------------------------------------------------------

def test_act_cutoff_window_is_local_and_may_wrap(tmp_path):
    cfg = _cfg(tmp_path, observatory_tz="America/Denver",
               pushover_act_cutoff_local="00:00", pushover_act_resume_local="08:00")
    # 2026-10-24 is MDT (UTC-6)
    assert po.act_held_now(cfg, datetime(2026, 10, 25, 5, 59)) is False   # 23:59
    assert po.act_held_now(cfg, datetime(2026, 10, 25, 6, 0)) is True     # 00:00
    assert po.act_held_now(cfg, datetime(2026, 10, 25, 13, 59)) is True   # 07:59
    assert po.act_held_now(cfg, datetime(2026, 10, 25, 14, 0)) is False   # 08:00
    wrap = _cfg(tmp_path, observatory_tz="America/Denver",
                pushover_act_cutoff_local="22:30", pushover_act_resume_local="07:00")
    assert po.act_held_now(wrap, datetime(2026, 10, 25, 4, 29)) is False  # 22:29
    assert po.act_held_now(wrap, datetime(2026, 10, 25, 4, 30)) is True   # 22:30
    assert po.act_held_now(wrap, datetime(2026, 10, 25, 12, 59)) is True  # 06:59
    same = _cfg(tmp_path, pushover_act_cutoff_local="00:00", pushover_act_resume_local="00:00")
    assert po.act_held_now(same, datetime(2026, 10, 25, 8, 0)) is False


def test_after_the_cutoff_only_safety_acts_buzz_the_rest_is_held(tmp_path, sent, monkeypatch):
    monkeypatch.setattr(po, "act_held_now", lambda c, now_utc=None: True)
    cfg = _cfg(tmp_path)
    assert run(po.notify(cfg, "Piggy-600 cooler STILL stalled after camera reconnect 1/2",
                         title="PhotonScript cooler recovery", kind="act")) is False
    assert run(po.notify(cfg, "Settings wrong: TheSky time zone 0", title="PhotonScript settings",
                         kind="act")) is False
    assert run(po.notify(cfg, "SAFETY STOP: park FAILED", title="PhotonScript SAFETY STOP",
                         kind="act", safety=True)) is True
    assert run(po.notify(cfg, "Roof reopened", title="PhotonScript roof", kind="story")) is True
    assert [s["title"] for s in sent] == ["PhotonScript SAFETY STOP", "PhotonScript roof"]
    held = [e for e in _story(cfg) if e["reason"] == "act-after-cutoff"]
    assert [e["title"] for e in held] == ["PhotonScript cooler recovery", "PhotonScript settings"]
    assert len(ns._held_acts(cfg, po.night_of(cfg))) == 2


# ---------------------------------------------------------------------------
# the plan heads-up
# ---------------------------------------------------------------------------

def _plan(cfg, now: datetime, **kw) -> dict:
    dusk = now + timedelta(hours=2)
    p = {"night_of": po.night_of(cfg, now),
         "preconfig_utc": (dusk - timedelta(minutes=30)).isoformat() + "Z",
         "dusk_utc": dusk.isoformat() + "Z",
         "dawn_utc": (dusk + timedelta(hours=10)).isoformat() + "Z",
         "dark_hours": 10.1, "targets": ["NGC 604", "M33"],
         "stats": {"est_integration_h": 8.2},
         "schedule": [
             {"name": "NGC 604", "fill": False,
              "window_start_utc": dusk.isoformat() + "Z",
              "window_end_utc": (dusk + timedelta(hours=3)).isoformat() + "Z",
              "filters": [{"f": "Ha", "n": 20, "exp": 300}, {"f": "OIII", "n": 20, "exp": 300}]},
             {"name": "M33", "fill": False,
              "window_start_utc": (dusk + timedelta(hours=3)).isoformat() + "Z",
              "window_end_utc": (dusk + timedelta(hours=8)).isoformat() + "Z",
              "filters": [{"f": "L", "n": 30, "exp": 180}, {"f": "R", "n": 10, "exp": 120},
                          {"f": "G", "n": 10, "exp": 120}, {"f": "B", "n": 10, "exp": 120}]},
             {"name": "Heart Nebula", "fill": True,
              "window_start_utc": (dusk + timedelta(hours=8)).isoformat() + "Z",
              "window_end_utc": (dusk + timedelta(hours=10)).isoformat() + "Z"}]}
    p.update(kw)
    return p


def test_plan_headsup_text_has_everything_to_change_before_bed(tmp_path):
    cfg = _cfg(tmp_path, observatory_tz="America/Denver")
    now = datetime(2026, 10, 24, 23, 0)          # 17:00 MDT
    text = ns.plan_headsup_text(
        cfg, _plan(cfg, now), guided=False,
        moon="Moon 95%: up at dusk, sets 04:10, broadband from 04:10",
        tpoint="TPoint: TPoint model 2026-10-23: 26 points, sky RMS 14.4\"",
        protrack="ProTrack: on", owed="Calibration owed: RC16 flats Ha, OIII",
        forecast="Tonight: GREEN - 10.2/10.2h usable",
        piggy="Piggy-600: OSC 450s riding the RC16",
        needs="Needs you before pre-config 18:30: ProTrack: OFF")
    assert text.startswith("Tonight 2026-10-24: imaging 19:00-05:00 (10.1 h dark), "
                           "pre-config 18:30. Change the plan before 18:30.")
    for bit in ("Needs you before pre-config 18:30: ProTrack: OFF",
                "RC16 unguided (TPoint + ProTrack): NGC 604 19:00-22:00 (Ha/OIII 20x300s)",
                "M33 22:00-03:00 (L 30x180s, R/G/B 10x120s)", "Heart Nebula fill 03:00-05:00",
                "Piggy-600: OSC 450s", "Moon 95%", "TPoint model 2026-10-23",
                "ProTrack: on", "Calibration owed: RC16 flats", "GREEN"):
        assert bit in text, bit
    assert len(text) <= 1024
    guided = ns.plan_headsup_text(cfg, _plan(cfg, now), guided=True, protrack="ProTrack: on")
    assert "RC16 guided (PHD2)" in guided and "ProTrack" not in guided


class _Armer:
    def __init__(self, cfg, plan, guided=False):
        self.config, self.plan, self.guided = cfg, plan, guided
        self.recipe, self.tpoint_pf, self.protrack_state = None, None, "off"

    def _use_guiding(self):
        return self.guided


@pytest.fixture
def quiet_plan_inputs(monkeypatch):
    from photonscript.scheduler import moon
    monkeypatch.setattr(moon, "moon_window_tonight", lambda c, now=None: {"available": False})
    monkeypatch.setattr(ns, "_owed_line", lambda c: "Calibration owed: RC16 flats Ha")


def test_plan_headsup_lead_once_folds_the_arm_time_acts(tmp_path, sent, quiet_plan_inputs):
    cfg = _cfg(tmp_path)
    now = datetime.utcnow()
    plan = _plan(cfg, now)          # pre-config in 90 min: inside the 90 min lead
    armer = _Armer(cfg, plan)
    ns.open_plan_window(cfg, plan)
    # the arm's own checks: one waits for the heads-up, a safety act does not
    assert run(po.notify(cfg, "Armed UNGUIDED but ProTrack is OFF in TheSky",
                         title="PhotonScript ProTrack", kind="act")) is False
    assert run(po.notify(cfg, "Dispatch FAILED: lint", title="PhotonScript ERROR",
                         kind="act", safety=True)) is True
    assert _story(cfg)[0]["reason"] == "for-plan"
    # too early (lead 30 min): nothing
    early = _cfg(tmp_path, pushover_plan_lead_min=30)
    assert run(ns.maybe_plan_headsup(_Armer(early, plan), now)) is False
    assert run(ns.maybe_plan_headsup(armer, now)) is True
    head = sent[-1]
    assert head["title"] == ns.TITLE_PLAN
    assert "Needs you before pre-config" in head["message"]
    assert "ProTrack: Armed UNGUIDED but ProTrack is OFF" in head["message"]
    assert "ProTrack: OFF, tick Activate ProTrack" in head["message"]
    # once per plan; the folded problem does not buzz again tonight
    assert run(ns.maybe_plan_headsup(armer, now + timedelta(minutes=5))) is False
    assert run(po.notify(cfg, "ProTrack still OFF", title="PhotonScript ProTrack",
                         kind="act")) is False
    assert _story(cfg)[-1]["reason"] == "dup-fact"
    # a changed plan is a new heads-up; after astro dark none
    plan2 = dict(plan, schedule=plan["schedule"][:1])
    assert run(ns.maybe_plan_headsup(_Armer(cfg, plan2), now + timedelta(minutes=6))) is True
    late = _Armer(cfg, dict(plan2, schedule=[]))
    assert run(ns.maybe_plan_headsup(late, now + timedelta(hours=3))) is False


def test_imaging_now_once_when_dark_and_open(tmp_path, sent):
    cfg = _cfg(tmp_path)
    now = datetime.utcnow()
    plan = _plan(cfg, now - timedelta(hours=2, minutes=5))   # dusk 5 min ago
    armer = _Armer(cfg, plan, guided=True)
    assert run(ns.maybe_imaging_now(armer, now, safe=False)) is False   # roof closed
    assert run(ns.maybe_imaging_now(armer, now, safe=True)) is True
    assert run(ns.maybe_imaging_now(armer, now + timedelta(minutes=1), safe=True)) is False
    assert sent[0]["title"] == ns.TITLE_IMAGING
    assert "RC16 guided on NGC 604 (Ha/OIII 20x300s)" in sent[0]["message"]
    assert "~8.2 h planned" in sent[0]["message"]


# ---------------------------------------------------------------------------
# first light, target switches
# ---------------------------------------------------------------------------

def test_first_light_per_rig_with_thumbnail_then_target_switch(tmp_path, sent, monkeypatch):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    night = po.night_of(cfg)
    monkeypatch.setattr(ns, "_thumb_path", lambda c, n, f: str(tmp_path / "t.png"))
    subs = [{"rig": "rc16", "target": "NGC 604", "passed_qa": True, "exp_s": 300}] * 3 + \
        [{"rig": "rc16", "target": "NGC 604", "passed_qa": False, "exp_s": 300}]
    from photonscript.scheduler import runs
    monkeypatch.setattr(runs, "_load_subs", lambda c, d: subs)
    rej = {"rig": "rc16", "target": "NGC 604", "filter": "Ha", "exp_s": 300,
           "passed_qa": False, "file": "LIGHT/a.fits", "time": "2026-10-25T02:10:00Z"}
    ok = dict(rej, passed_qa=True, file="LIGHT/b.fits", hfr=2.31, ecc=0.42, stars=1240)
    assert run(ns.after_sub(cfg, night, rej, "rc16")) == {"first_light": False, "switch": False}
    assert run(ns.after_sub(cfg, night, ok, "rc16"))["first_light"] is True
    assert run(ns.after_sub(cfg, night, dict(ok, file="LIGHT/c.fits"), "rc16"))["first_light"] is False
    pig = dict(ok, rig="piggyback", filter="OSC", exp_s=450, file="P/a.fits")
    assert run(ns.after_sub(cfg, night, pig, "piggyback"))["first_light"] is True
    m33 = dict(ok, target="M33", filter="L", exp_s=180, file="LIGHT/d.fits")
    assert run(ns.after_sub(cfg, night, m33, "rc16"))["switch"] is True
    assert run(ns.after_sub(cfg, night, dict(m33, file="LIGHT/e.fits"), "rc16"))["switch"] is False
    titles = [s["title"] for s in sent]
    assert titles == [ns.TITLE_FIRST, ns.TITLE_FIRST, ns.TITLE_TARGET]
    assert "First light RC16" in sent[0]["message"] and "HFR 2.31 px, ecc 0.42, 1240 stars" in sent[0]["message"]
    assert sent[0]["attachment"] == str(tmp_path / "t.png")
    assert "First light Piggy-600" in sent[1]["message"]
    assert sent[2]["message"].startswith("RC16 now on M33 (L 180s")
    assert "after NGC 604: 3 kept / 1 rejected, 0.2 h" in sent[2]["message"]
    fl = [e for e in _story(cfg) if e["title"] == ns.TITLE_FIRST]
    assert fl[0]["thumb"] == f"/api/runs/{night}/thumb?file=LIGHT/b.fits&w=720"


def test_test_subs_never_make_first_light(tmp_path, sent):
    cfg = _cfg(tmp_path)
    rec = {"rig": "rc16", "target": "Tracking test", "test": True, "passed_qa": True,
           "file": "x.fits"}
    assert run(ns.after_sub(cfg, po.night_of(cfg), rec, "rc16")) == {"first_light": False,
                                                                     "switch": False}
    assert sent == []


# ---------------------------------------------------------------------------
# the roof, debounced
# ---------------------------------------------------------------------------

def test_roof_reopen_is_one_story_per_long_closure(tmp_path, sent):
    cfg = _cfg(tmp_path)
    t0 = datetime.utcnow()
    plan = _plan(cfg, t0 - timedelta(hours=3))         # dark since an hour ago
    ns._update(cfg, plan["night_of"], lambda st: st.update(imaging_t="x"))  # night started
    assert run(ns.roof_tick(cfg, False, t0, plan)) is None
    assert run(ns.roof_tick(cfg, False, t0 + timedelta(minutes=2), plan)) is None
    short = run(ns.roof_tick(cfg, True, t0 + timedelta(minutes=4), plan))
    assert short["minutes"] == 4.0 and sent == []     # under the 10 min debounce
    t1 = t0 + timedelta(minutes=20)
    run(ns.roof_tick(cfg, False, t1, plan))
    run(ns.roof_tick(cfg, None, t1 + timedelta(minutes=10), plan))   # unreadable: no change
    long = run(ns.roof_tick(cfg, True, t1 + timedelta(minutes=72), plan))
    assert long["minutes"] == 72.0
    assert [s["title"] for s in sent] == [ns.TITLE_ROOF]
    assert "after 1 h 12 min closed" in sent[0]["message"] and "of dark left" in sent[0]["message"]
    kinds = [(e["kind"], e["sent"]) for e in _story(cfg) if e["title"] == ns.TITLE_ROOF]
    assert kinds == [("info", False), ("info", False), ("info", False), ("story", True)]
    line = ns.roof_timeline(cfg, plan["night_of"], None, None)
    assert line.count("closed") == 2 and "(1 h 12 min)" in line and "(4 min)" in line


def test_roof_reopen_before_imaging_started_is_info(tmp_path, sent):
    cfg = _cfg(tmp_path)
    t0 = datetime.utcnow()
    plan = _plan(cfg, t0 - timedelta(hours=3))
    run(ns.roof_tick(cfg, False, t0, plan))
    run(ns.roof_tick(cfg, True, t0 + timedelta(minutes=50), plan))
    assert sent == []      # "imaging now" says the night started


def test_roof_ignored_outside_the_dark_window_and_for_good_near_dawn(tmp_path, sent):
    cfg = _cfg(tmp_path)
    now = datetime.utcnow()
    plan = _plan(cfg, now)                           # dark starts in 2 h
    assert run(ns.roof_tick(cfg, False, now, plan)) is None
    assert ns.load_state(cfg, plan["night_of"]).get("roof") is None
    dawn_soon = dict(plan, dawn_utc=(now + timedelta(minutes=30)).isoformat() + "Z")
    assert ns.roof_closed_for_good(dawn_soon, now, False) is True
    assert ns.roof_closed_for_good(dawn_soon, now, None) is False
    assert ns.roof_closed_for_good(plan, now, False) is False


# ---------------------------------------------------------------------------
# the end-of-night report
# ---------------------------------------------------------------------------

def _night_subs():
    s = []
    for i in range(10):
        s.append({"rig": "rc16", "target": "NGC 604", "passed_qa": i < 8, "exp_s": 300,
                  "hfr": 2.0 + i / 10, "ecc": 0.40, "file": f"L/{i}.fits",
                  "drivers": [] if i < 8 else ["ecc"]})
    for i in range(4):
        s.append({"rig": "piggyback", "target": "NGC 604", "passed_qa": True,
                  "exp_s": 450, "hfr": 3.1, "ecc": 0.5, "file": f"P/{i}.fits"})
    s.append({"rig": "rc16", "target": "Tracking test", "test": True, "passed_qa": True,
              "exp_s": 60, "file": "T/0.fits"})
    return s


def test_end_of_night_report_says_what_the_night_gave(tmp_path, monkeypatch, sent):
    cfg = _cfg(tmp_path, piggyback_enabled=True)
    night = po.night_of(cfg)
    from photonscript.scheduler import dusk_flats_check as dfc
    monkeypatch.setattr(dfc, "morning_line",
                        lambda c, d: f"Dusk flats {d}: RC16 6 shot, 6 filed")
    monkeypatch.setattr(dfc, "dawn_morning_line",
                        lambda c, d, times=None: f"Dawn flats {d}: RC16 0 shot, 0 filed "
                                                 "(roof closed at the flat window)")
    monkeypatch.setattr(ns, "_owed_line", lambda c: "Calibration owed: Piggy-600 darks 450 s")
    monkeypatch.setattr(ns, "_doctor_verdict", lambda c, n, r: "sub doctor: tracking_trail"
                        if r == "rc16" else "sub doctor: healthy")
    po.story_append(cfg, "act", "PhotonScript cooler recovery", "Piggy cooler stalled",
                    sent=False, reason="act-after-cutoff")

    class P:
        target = type("T", (), {"name": "NGC 604"})()
        total_integration_hours = 12.3
        budget_hours = 20
    rep = ns.end_of_night_report(cfg, night, reason="dawn", subs=_night_subs(), projects=[P()])
    t = rep["text"]
    assert t.startswith(f"Night {night} over (dawn, ")
    assert "12 kept of 14 subs, 1.2 h added." in t
    assert "RC16: NGC 604 10 shot, 8 kept, 2 rej (top ecc 2), +0.7 h (goal 12.3/20 h)" in t
    assert "Piggy-600: NGC 604 4 shot, 4 kept, 0 rej, +0.5 h" in t
    assert "RC16 stars: median HFR 2.35 px, ecc 0.40, sub doctor: tracking_trail" in t
    assert "Roof: open all night" in t
    assert "Dusk flats" in t and "Dawn flats" in t and "roof closed at the flat window" in t
    assert "Held overnight for you:" in t and "Piggy cooler stalled" in t
    assert "Calibration owed: Piggy-600 darks" in t
    assert rep["thumb_file"] == "L/0.fits"           # the best kept RC16 sub
    assert len(t) <= 1024


def test_end_of_night_pushes_once_with_a_thumbnail(tmp_path, monkeypatch, sent):
    cfg = _cfg(tmp_path)
    night = po.night_of(cfg)
    monkeypatch.setattr(ns, "end_of_night_report", lambda c, n, **k: {
        "text": f"Night {n} over", "kept": 40, "shot": 44, "thumb_file": "L/1.fits"})
    monkeypatch.setattr(ns, "_thumb_path", lambda c, n, f: "best.png")
    assert run(ns.end_of_night(cfg, night, reason="roof closed for good")) is True
    assert run(ns.end_of_night(cfg, night, reason="dawn")) is False
    assert sent[0]["title"] == ns.TITLE_END and sent[0]["attachment"] == "best.png"
    assert run(ns.end_of_night(cfg, night + "x", reason="dawn",
                               shutdown_failed=["park"])) is True
    assert sent[-1]["title"] == "PhotonScript shutdown warning"
    # the dawn filing: a follow-up only when the kept count moved materially
    assert ns.filing_kind(cfg, night, 42) == "info"
    assert ns.filing_kind(cfg, night, 47) == "story"
    assert ns.filing_kind(cfg, "2026-01-01", 3) == "story"   # no report went out


# ---------------------------------------------------------------------------
# watchers: a known reason stays quiet
# ---------------------------------------------------------------------------

def test_quiet_reason_knows_an_operator_run_the_roof_and_a_disarm(tmp_path, monkeypatch):
    from photonscript.telescope_agent import tpoint_import as ti
    monkeypatch.setattr(ti, "DEFAULT_IMPORT_DIR", str(tmp_path / "tpoint_import"))
    cfg = _cfg(tmp_path)
    now = datetime.utcnow()
    assert ns.quiet_reason(cfg, now) is None
    csv = tmp_path / "runs" / "2026-10-23_tpoint_autocal.csv"
    csv.parent.mkdir(parents=True)
    csv.write_text("utc,point\n")
    assert ns.quiet_reason(cfg, now).startswith("TPoint autocal run in progress")
    import os
    import time
    old = time.time() - 5 * 3600
    os.utime(csv, (old, old))
    assert ns.quiet_reason(cfg, datetime.utcnow()) is None
    imp = tmp_path / "tpoint_import" / "20261024T031500Z_west.dat"
    imp.parent.mkdir()
    imp.write_text("!TheSky Version 10.5.0")       # autocal's import file
    assert ns.quiet_reason(cfg, datetime.utcnow()).startswith("TPoint autocal")
    os.utime(imp, (old, old))
    from photonscript.shared import safety_history
    safety_history.record(cfg, False, now=now)
    assert ns.quiet_reason(cfg, now) == "roof closed"
    safety_history.record(cfg, True, now=now + timedelta(seconds=1))
    assert ns.quiet_reason(cfg, now) is None
    from photonscript.scheduler import arm_memory
    arm_memory.record_manual_disarm(cfg, now=now)
    assert ns.quiet_reason(cfg, now) == "disarmed by hand tonight"


def test_tpoint_no_samples_alarm_stays_quiet_with_a_known_reason(tmp_path, monkeypatch, sent):
    """2026-10-23: Jeremy opened a fresh empty TPoint file while
    tpoint-autocal wrote import files: "no samples in 15 min" was false."""
    from photonscript.scheduler import tpoint_watch as tw
    cfg = _cfg(tmp_path, tpoint_watch_mode="alert")
    alert = "TPoint run started but no samples in 15 min (roof closed? camera? Image Link?)"
    monkeypatch.setattr(tw, "step", lambda c, now=None, listing=None: {
        "state": {}, "push": None, "alert": alert})
    monkeypatch.setattr(ns, "quiet_reason", lambda c, now=None, **k: "TPoint autocal run in progress")
    res = run(tw.tick(cfg, listing={}))
    assert res["quiet"] == "TPoint autocal run in progress" and sent == []
    held = _story(cfg)[-1]
    assert held["reason"] == "quiet-known-reason" and "quiet: TPoint autocal" in held["text"]
    monkeypatch.setattr(ns, "quiet_reason", lambda c, now=None, **k: None)
    run(tw.tick(cfg, listing={}))
    assert [s["title"] for s in sent] == ["PhotonScript TPoint run"]
    assert "If you act: check the roof, the camera and Image Link" in sent[0]["message"]


# ---------------------------------------------------------------------------
# endpoints
# ---------------------------------------------------------------------------

def test_story_endpoint_mirrors_the_storyline(tmp_path, sent, monkeypatch):
    from photonscript.scheduler.routers import night_story as rt
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(rt, "_cfg", lambda: cfg)
    run(po.notify(cfg, "Tonight ...", title=ns.TITLE_PLAN, kind="story"))
    run(po.notify(cfg, "First light RC16", title=ns.TITLE_FIRST, kind="story",
                  thumb="/api/runs/x/thumb?file=a&w=720"))
    run(po.notify(cfg, "ProTrack OFF", title="PhotonScript ProTrack", kind="act"))
    run(po.notify(cfg, "frozen EAF", title="PhotonScript frozen sensor"))
    out = rt.api_night_story()
    assert out["night"] == po.night_of(cfg)
    assert [e["kind"] for e in out["entries"]] == ["story", "story", "act", "info"]
    assert out["entries"][1]["thumb"] == "/api/runs/x/thumb?file=a&w=720"
    assert out["pushed"] == 3 and out["by_kind"]["info"] == {"pushed": 0, "logged": 1}
    only = rt.api_night_story(kinds="act,story", sent_only=True)
    assert [e["title"] for e in only["entries"]] == [ns.TITLE_PLAN, ns.TITLE_FIRST,
                                                     "PhotonScript ProTrack"]
    assert all(set(e) >= {"t", "kind", "title", "text", "sent"} for e in out["entries"])


def test_weekly_counts_per_night_and_kind(tmp_path, sent, monkeypatch):
    from photonscript.scheduler.routers import night_story as rt
    cfg = _cfg(tmp_path)
    monkeypatch.setattr(rt, "_cfg", lambda: cfg)
    run(po.notify(cfg, "a", title="PhotonScript roof", kind="story"))
    run(po.notify(cfg, "b", title="PhotonScript ProTrack", kind="act"))
    run(po.notify(cfg, "c", title="PhotonScript heartbeat"))
    with open(po._audit_path(cfg), "a", encoding="utf-8") as f:   # a pre-v2 row
        f.write(json.dumps({"ts": datetime.utcnow().isoformat() + "+00:00",
                            "title": "PhotonScript armed", "sent": True,
                            "reason": "sent", "priority": 0, "message": "x"}) + "\n")
    out = rt.api_pushover_weekly(days=7)
    n = out["nights"][po.night_of(cfg)]
    assert n["sent"] == 3 and n["held"] == 1
    assert n["by_kind"]["legacy"] == {"sent": 1, "held": 0}
    assert n["by_kind"]["info"] == {"sent": 0, "held": 1}
    assert out["sent_total"] == 3 and dict(out["held_reasons"]) == {"info": 1}


# ---------------------------------------------------------------------------
# NINA's own Send to Pushover items
# ---------------------------------------------------------------------------

def test_nina_push_items_are_annotations_unless_turned_on(monkeypatch):
    from photonscript.scheduler import nina_sequence_json as nsj
    item = nsj._pushover("Flats", "dusk sky flats run ended")
    assert "Annotation" in item["$type"] and item["Text"] == "[Flats] dusk sky flats run ended"
    monkeypatch.setenv("PS_NINA_PUSHOVER_ITEMS", "true")
    monkeypatch.setattr(nsj, "_gen_cfg_cache", None)
    item = nsj._pushover("Flats", "dusk sky flats run ended")
    assert "SendToPushover" in item["$type"] and item["Message"] == "dusk sky flats run ended"


# ---------------------------------------------------------------------------
# replay: the 2026-10-16..23 audit log through the policy
# ---------------------------------------------------------------------------

def test_replay_last_week_through_the_policy(tmp_path):
    """The scope's notifications.jsonl of 2026-10-16 to 10-24 (GET
    /api/notifications, 427 records, 314 sent) through the v2 policy: each
    record gets the kind its migrated call site carries (ps212_replay), the
    non-safety acts of an arm wait for the plan heads-up, the TPoint watcher
    alarm of 10-23 has its known reason. Then the v2 story pushes a night
    would add (plan, imaging now, first light per rig, switches, flats,
    roof reopens) are counted on top."""
    from tests.test_scheduler import ps212_replay as rp
    cfg = _cfg(tmp_path, observatory_tz="America/Denver",
               pushover_act_cutoff_local="00:00", pushover_act_resume_local="08:00")
    recs = rp.load()
    assert len(recs) == 427
    out = rp.simulate(cfg, recs, windows=rp.plan_windows(cfg, recs),
                      quiet=lambda r: ("TPoint autocal run in progress"
                                       if r["title"] == "PhotonScript TPoint run" else None))
    nights = rp.imaging_nights(recs, cfg)
    assert len(nights) == 7

    def in_night(o):      # 15:00 to 09:00 local (MDT): the imaging night
        loc = rp._ts(o) - timedelta(hours=6)
        return loc.hour >= 15 or loc.hour < 9
    totals = {}
    for n, info in nights.items():
        before = sum(1 for r in recs if r["sent"] and po.night_of(cfg, rp._ts(r)) == n
                     and in_night(r))
        after = sum(1 for o in out if o["sent"] and o["night"] == n and in_night(o))
        totals[n] = (before, after + rp.story_pushes(info))
    pushes = sorted(t for _b, t in totals.values())
    # about 10 a night (the story count is an upper bound: every armed
    # target reached, both rigs' first light): the median at most 11, the
    # quiet nights 7, the worst (the 10-16 shakedown night with two failed
    # dispatches, the 10-23 TPoint night armed after dark) at most 16;
    # every night far under what it was (30 to 56)
    assert pushes[len(pushes) // 2] <= 11, totals
    assert pushes[0] <= 8, totals
    assert max(pushes) <= 16, totals
    assert all(a <= 0.45 * b for b, a in totals.values()), totals
    # no duplicate facts: a non-safety fact is pushed at most once a night
    seen = {}
    for o in out:
        if o["sent"] and not o["safety"]:
            k = (o["night"], o["fact"])
            assert k not in seen, k
            seen[k] = o
    # every safety act still sent (bar the limiter's 5 min dedup of the
    # very same text, which is not a new fact)
    for o in out:
        if o["safety"]:
            assert o["sent"] or o["reason"] == "dedup", o
    assert sum(1 for o in out if o["safety"] and o["sent"]) >= 5
    # the 10-23 false alarm stays quiet; after midnight nothing non-safety buzzes
    assert any(o["reason"].startswith("quiet:") for o in out)
    for o in out:
        loc = rp._ts(o) - timedelta(hours=6)
        if o["sent"] and o["kind"] == "act" and loc.hour < 8:
            assert o["safety"], o
