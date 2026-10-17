"""Re-entry gates (2026-10-15): skip a target's start AF (and, in "on", the
slew / center) when nothing changed since the last one.

Covers the 2026-10-14 replay (NINA #1 event history, read-only GET of that
night: AF at 22:21, 22:29, 22:35 and a cut one at 22:41, all at 5719), the
guards (temperature, frozen sensor, filter offsets, old / failed AF, no
report, a refused focuser move), the slew / center decision, the generated
sequence with and without gates, the lint rule and the dispatch gate, the
PS-77 sequence simulator (a SAFE_LOOP re-entry after a park skips the start
AF) and the CLI / cmd exit codes."""
import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from photonscript.scheduler import dispatch_gate
from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler import reentry
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget

from tests.test_scheduler.test_ps77_safety_stop import (
    NinaSim, _Interrupt, _walk)

FIX = Path(__file__).parent / "fixtures"
EVENTS_1014 = json.loads((FIX / "stuck_loop" / "2026-10-14.json").read_text(
    encoding="utf-8"))["nina_events"]["rc16"]
REPORTS_1014 = json.loads((FIX / "reentry" / "2026-10-14_af_reports.json")
                          .read_text(encoding="utf-8"))["reports"]
TEMP_1014 = 18.29


def _cfg(tmp_path=None, **kw):
    kw.setdefault("reentry_optimize", "af")   # the skip tests exercise af mode
    kw.setdefault("focus_frozen_temps", "21.29")
    kw.setdefault("focus_filter_offsets", "Ha:120,OIII:120,SII:120")
    if tmp_path is not None:
        kw.setdefault("data_dir", str(tmp_path))
    return PhotonScriptConfig(**kw)


def _utc(local: str) -> datetime:
    """'22:29:33' on 2026-10-14 MDT -> naive UTC."""
    t = datetime.fromisoformat(f"2026-10-14T{local}")
    return t + timedelta(hours=6)


def _upto(t_utc: datetime, events=EVENTS_1014, cfg=None):
    cfg = cfg or _cfg()
    out = []
    for e in events:
        t = reentry._event_time(cfg, e.get("Time"))
        if t is not None and t < t_utc:
            out.append(e)
    return out


def _decide(now, cfg=None, events=None, reports=REPORTS_1014, temp=TEMP_1014,
            filt="L", frozen=(False, "")):
    cfg = cfg or _cfg()
    ev = _upto(now, cfg=cfg) if events is None else events
    return reentry.af_decision(cfg, now=now, events=ev, reports=reports,
                               temp_now=temp, filter_name=filt, frozen=frozen)


# --- the 2026-10-14 replay ----------------------------------------------------

AF_STARTS_1014 = ["22:21:49", "22:29:33", "22:35:43", "22:41:51"]


def _af_runs_1014(cfg):
    """(start, end) naive UTC of each AF NINA #1 ran on 10-14; the 22:41 one
    never finished (it stopped at the center at 22:43:35)."""
    from photonscript.scheduler.af_stall import af_runs
    rows = [{**e, "Time": reentry._event_time(cfg, e["Time"]).isoformat()}
            for e in EVENTS_1014]
    out = []
    for r in af_runs(rows):
        end = r["end"] or _utc("22:43:35")
        out.append((r["start"], end))
    return out


def test_replay_1014_skips_every_af_after_the_first():
    cfg = _cfg()
    verdicts = [_decide(_utc(t), cfg) for t in AF_STARTS_1014]
    assert [v["skip"] for v in verdicts] == [False, True, True, True]
    assert "no finished AF" in verdicts[0]["why"]
    for v in verdicts[1:]:
        assert v["position"] == 5719 and v["target"] == 5719
        assert v["af_filter"] == "L" and v["dt"] == 0.0
        assert v["age_min"] < 5
        assert v["event_best"] == 5719       # the history agrees with the report


def test_replay_1014_time_saved():
    """The three re-entry AFs NINA #1 ran after the first took 3.3 + 3.2 +
    1.7 min (the last one cut by the center); a full AF is ~3.3 min."""
    cfg = _cfg()
    runs = _af_runs_1014(cfg)
    assert len(runs) == 4
    skipped = [(b - a).total_seconds() / 60 for a, b in runs[1:]]
    assert [round(x, 1) for x in skipped] == [3.3, 3.2, 1.7]
    assert sum(skipped) > 8.0
    full = sum((b - a).total_seconds() for a, b in runs[:3]) / 3 / 60
    assert 3.2 < full < 3.4


# --- guards -------------------------------------------------------------------

NOW = _utc("22:41:51")          # last AF 22:38:57


def test_temperature_moved_runs_the_af():
    v = _decide(NOW, temp=TEMP_1014 + 1.4)
    assert not v["skip"] and "+1.40 > 1 C" in v["why"]
    assert _decide(NOW, temp=TEMP_1014 - 0.9)["skip"]


def test_frozen_sensor_falls_back_to_time_only():
    cfg = _cfg()
    fr = reentry.frozen_sensor(cfg, 21.29)
    assert fr[0] and "focus_frozen_temps" in fr[1]
    v = _decide(NOW, cfg, temp=21.29, frozen=fr)   # report says 18.29
    assert v["skip"] and v["dt"] is None and "frozen" in v["why"]
    # frozen but the AF is old: time still rules
    assert not _decide(NOW + timedelta(minutes=50), cfg, temp=21.29,
                       frozen=fr)["skip"]


def test_frozen_by_track_or_by_af_reports():
    cfg = _cfg()
    assert reentry.frozen_sensor(cfg, 18.29, unchanged_min=7 * 60)[0]
    assert not reentry.frozen_sensor(cfg, 18.29, unchanged_min=5 * 60)[0]
    pts = [{"time": "2026-10-14T13:00:00Z", "temp": 18.29},
           {"time": "2026-10-14T20:00:00Z", "temp": 18.30}]
    now = datetime(2026, 10, 14, 21, 0)
    assert reentry.frozen_sensor(cfg, 18.29, None, pts, now)[0]
    pts[1]["temp"] = 18.6
    assert not reentry.frozen_sensor(cfg, 18.29, None, pts, now)[0]
    assert reentry.frozen_sensor(cfg, None) == (False, "no focuser temperature")


def test_old_af_runs_the_af():
    v = _decide(_utc("22:38:57") + timedelta(minutes=46))
    assert not v["skip"] and "46 min ago" in v["why"]
    assert _decide(_utc("22:38:57") + timedelta(minutes=44))["skip"]


def test_limits_come_from_config():
    cfg = _cfg(reentry_af_skip_min=2.0, reentry_af_temp_c=0.05)
    assert not _decide(NOW, cfg)["skip"]          # 2.9 min > 2
    cfg = _cfg(reentry_af_temp_c=0.05)
    assert not _decide(NOW, cfg, temp=TEMP_1014 + 0.1)["skip"]


def test_filter_offsets():
    cfg = _cfg()
    # AF in L, the target starts in Ha: configured offset +120
    v = _decide(NOW, cfg, filt="Ha")
    assert v["skip"] and v["target"] == 5719 + 120
    # AF in R (no measured, no configured offset) -> unknown
    reps = [{**r, "Filter": "R"} for r in REPORTS_1014]
    v = _decide(NOW, cfg, reports=reps)
    assert not v["skip"] and "no known focus offset for R" in v["why"]
    # AF in Ha, start filter L: the offset back is known
    reps = [{**r, "Filter": "Ha"} for r in REPORTS_1014]
    v = _decide(NOW, cfg, reports=reps)
    assert v["skip"] and v["target"] == 5719 - 120


def test_no_report_or_bad_fit_runs_the_af():
    v = _decide(NOW, reports=[])
    assert not v["skip"] and "no NINA AF report" in v["why"]
    bad = [{**r, "RSquares": {"Hyperbolic": 0.3}} for r in REPORTS_1014]
    assert not _decide(NOW, reports=bad)["skip"]
    nan = [{**r, "CalculatedFocusPoint": {"Position": float("nan")}}
           for r in REPORTS_1014]
    assert not _decide(NOW, reports=nan)["skip"]


def test_a_later_failed_af_runs_the_af():
    ev = _upto(NOW) + [
        {"Time": "2026-10-14T22:40:00-06:00", "Event": "AUTOFOCUS-STARTING"},
        {"Time": "2026-10-14T22:40:20-06:00", "Event": "AUTOFOCUS-POINT-ADDED",
         "HFR": 0, "Position": 6000},
        {"Time": "2026-10-14T22:40:40-06:00", "Event": "AUTOFOCUS-FAILED"}]
    v = _decide(NOW, events=ev)
    assert not v["skip"] and "later AF" in v["why"]


def test_an_af_cut_by_the_restart_is_ignored():
    """22:41's AF stopped at 4 points (no FINISHED): the next decision still
    uses 22:38:57's."""
    v = _decide(_utc("22:44:00"), events=_upto(_utc("22:44:00")))
    assert v["skip"]
    assert abs((v["af_t"] - _utc("22:38:57")).total_seconds()) < 1


def test_nina_restart_means_no_history():
    assert not _decide(NOW, events=[])["skip"]


# --- the live gate (fake ninaAPI) ---------------------------------------------

class FakeIO:
    def __init__(self, events=None, temp=TEMP_1014, reports=REPORTS_1014,
                 move_ok=True, land=None, mount=None, solve=None):
        self.events = _upto(NOW) if events is None else events
        self.temp, self._reports = temp, reports
        self.move_ok, self.land = move_ok, land
        self.pos, self.moves = 5839, []
        self.mount, self._solve = mount, solve

    async def get(self, path):
        if path == "/event-history":
            return self.events
        if path == "/equipment/focuser/info":
            return {"Position": self.pos, "Temperature": self.temp,
                    "IsMoving": False}
        if path == "/equipment/weather/info":
            return {"Temperature": 12.0}
        if path == "/equipment/mount/info":
            return self.mount
        return None

    async def move(self, position):
        self.moves.append(position)
        if not self.move_ok:
            return None
        self.pos = self.land if self.land is not None else position
        return "Moving"

    async def reports(self):
        return list(self._reports)

    async def solve(self, night, ra_deg, dec_deg, now):
        return self._solve

    async def sleep(self, s):
        return None


def _gate(cfg, io, kind="af", **kw):
    return asyncio.run(reentry.run_gate(cfg, kind, kw.pop("label", "NGC 604"),
                                        io=io, now=kw.pop("now", NOW), **kw))


def _events(tmp_path):
    p = tmp_path / "runs" / "2026-10-14_events.jsonl"
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()]


def test_gate_skips_after_moving_the_focuser(tmp_path):
    cfg = _cfg(tmp_path)
    io = FakeIO()
    out = _gate(cfg, io, filter_name="L")
    assert out["verdict"] == "SKIP" and io.moves == [5719]
    ev = _events(tmp_path)
    assert ev[-1]["kind"] == "reentry" and ev[-1]["value"] == "skip_af"
    assert ev[-1]["saved_s"] == 200.0 and ev[-1]["target"] == 5719
    line = reentry.morning_lines(cfg, "2026-10-14")[0]
    assert line.startswith("Re-entry: 1 skip(s), ~3 min saved") and "AF" in line


def test_gate_runs_when_the_move_fails_or_lands_wrong(tmp_path):
    cfg = _cfg(tmp_path)
    assert _gate(cfg, FakeIO(move_ok=False), filter_name="L")["verdict"] == "RUN"
    out = _gate(cfg, FakeIO(land=5790), filter_name="L")
    assert out["verdict"] == "RUN" and "AF runs" in out["why"]
    assert [e["value"] for e in _events(tmp_path)] == ["run_af", "run_af"]


def test_gate_modes(tmp_path):
    io = FakeIO()
    out = _gate(_cfg(tmp_path, reentry_optimize="advise"), io, filter_name="L")
    assert out["verdict"] == "RUN" and io.moves == []
    assert out["why"].startswith("advise (would skip)")
    assert _events(tmp_path)[-1]["value"] == "advise_af"
    assert _gate(_cfg(tmp_path, reentry_optimize="off"), FakeIO(),
                 filter_name="L")["verdict"] == "RUN"
    assert _gate(_cfg(tmp_path), FakeIO(), kind="bogus")["verdict"] == "RUN"
    lines = reentry.morning_lines(_cfg(tmp_path), "2026-10-14")
    assert "advise: 1 would have skipped" in lines[0]


def test_gate_temperature_moved_runs(tmp_path):
    out = _gate(_cfg(tmp_path), FakeIO(temp=20.0), filter_name="L")
    assert out["verdict"] == "RUN" and "C at the AF" in out["why"]


# --- slew / center --------------------------------------------------------------

RA_H, DEC = 1.5780, 30.7800      # NGC 604 (J2000)


def _mount(sep_arcmin=0.5, **kw):
    from photonscript.scheduler.off_target import precess_from_j2000
    ra, dec = precess_from_j2000(RA_H * 15, DEC, NOW)
    m = {"Connected": True, "AtPark": False, "Slewing": False,
         "TrackingEnabled": True, "RightAscension": ra / 15.0,
         "Declination": dec + sep_arcmin / 60.0}
    m.update(kw)
    return m


def _solve(sep_arcmin=0.8, age_min=6.0):
    end = NOW - timedelta(minutes=age_min)
    return {"ra_deg": RA_H * 15, "dec_deg": DEC + sep_arcmin / 60.0,
            "start": end - timedelta(minutes=2), "end": end,
            "file": "LIGHT/x_NGC 604_L_120.00s_0004.fits"}


def _acq(mount, solve, events=()):
    return reentry.acquire_decision(_cfg(), now=NOW, events=list(events),
                                    mount=mount, ra_hours=RA_H, dec_deg=DEC,
                                    solve=solve)


def test_acquire_skips_both_on_target_with_a_fresh_solve():
    d = _acq(_mount(), _solve())
    assert d["skip_slew"] and d["skip_center"]
    assert d["solve_arcmin"] == pytest.approx(0.8, abs=0.01)


def test_acquire_recenters_when_the_pointing_error_is_unknown():
    for solve, why in ((None, "re-center only"), (_solve(sep_arcmin=3.0), "3.0'"),
                       (_solve(age_min=70), "70 min old")):
        d = _acq(_mount(), solve)
        assert d["skip_slew"] and not d["skip_center"] and why in d["why"]
    # the mount moved (park / center / flip) after the solved light
    moved = [{"Time": "2026-10-14T22:40:00-06:00", "Event": "MOUNT-PARKED"}]
    d = _acq(_mount(), _solve(), moved)
    assert d["skip_slew"] and not d["skip_center"] and "moved" in d["why"]


def test_acquire_runs_unless_tracking_on_target():
    for m, why in ((_mount(AtPark=True), "parked"),
                   (_mount(TrackingEnabled=False), "not tracking"),
                   (_mount(Slewing=True), "slewing"),
                   (_mount(sep_arcmin=25), "from the target"),
                   (None, "unreadable"),
                   (_mount(Connected=False), "not connected")):
        d = _acq(m, _solve())
        assert not d["skip_slew"] and not d["skip_center"] and why in d["why"]


def test_center_gate_follows_the_slew_gate(tmp_path):
    cfg = _cfg(tmp_path, reentry_optimize="on")
    io = FakeIO(events=[], mount=_mount(), solve=_solve())
    assert _gate(cfg, io, "slew", ra_hours=RA_H, dec_deg=DEC)["verdict"] == "SKIP"
    assert _gate(cfg, io, "center")["verdict"] == "SKIP"
    assert _gate(cfg, io, "center")["verdict"] == "RUN"   # used once
    io = FakeIO(mount=_mount(), solve=None)
    assert _gate(cfg, io, "slew", ra_hours=RA_H, dec_deg=DEC)["verdict"] == "SKIP"
    assert _gate(cfg, io, "center")["verdict"] == "RUN"   # cheap re-center
    # "af" mode never skips the acquire
    cfg = _cfg(tmp_path, reentry_optimize="af")
    io = FakeIO(mount=_mount(), solve=_solve())
    assert _gate(cfg, io, "slew", ra_hours=RA_H, dec_deg=DEC)["verdict"] == "RUN"
    assert _gate(cfg, io, "center")["verdict"] == "RUN"


# --- the generated sequence ---------------------------------------------------------

@pytest.fixture
def no_moon(monkeypatch):
    monkeypatch.setattr(nsj, "_moon_window", lambda: {
        "available": True, "down_at_dusk": False, "illum_pct": 5})


@pytest.fixture
def script(tmp_path, monkeypatch):
    p = tmp_path / "reentry-gate.cmd"
    p.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_REENTRY_GATE_SCRIPT", str(p))
    return p


def _ngc604(guided=False, count=48):
    t = NinaSequenceTarget(name="NGC 604", ra_hours=RA_H, dec_degrees=DEC,
                           exposures=[ExposurePlan(
                               filter_type=FilterType.LUMINANCE,
                               exposure_seconds=120, count=count, gain=100,
                               offset=50)])
    t.start_guiding = guided
    return t


def _gen(monkeypatch, mode, dusk=False, targets=None):
    monkeypatch.setenv("PS_REENTRY_OPTIMIZE", mode)
    seq = build_sequence_for_night("RE", targets or [_ngc604()])
    if dusk:
        seq.wait_until_local = "19:30:00"
    return json.loads(nsj.generate_nina_json(seq))


def _gated(seq):
    return {d["Name"]: d for d in _walk(seq)
            if str(d.get("Name", "")).endswith("(re-entry gate)")}


def _strip_ids(seq):
    s = json.dumps(seq)
    import re
    return re.sub(r'"\$(id|ref)": "\d+"', "", s)


def test_off_and_missing_script_are_the_old_sequence(monkeypatch, no_moon,
                                                     script):
    off = _gen(monkeypatch, "off")
    assert not _gated(off)
    monkeypatch.setenv("PS_REENTRY_GATE_SCRIPT", str(script) + ".missing")
    missing = _gen(monkeypatch, "af")
    assert not _gated(missing)
    assert "Re-entry gates OFF tonight" in json.dumps(missing)
    # the same items apart from that one annotation
    ann = [d for d in _walk(missing) if "Re-entry gates OFF" in str(d.get("Text"))]
    assert len(ann) == 1


def test_af_mode_gates_only_the_start_af(monkeypatch, no_moon, script):
    seq = _gen(monkeypatch, "af")
    g = _gated(seq)
    assert list(g) == ["NGC 604 start AF (re-entry gate)"]
    items = g["NGC 604 start AF (re-entry gate)"]["Items"]["$values"]
    kinds = [i["$type"].split(",")[0].split(".")[-1] for i in items]
    assert kinds[0] == "ExternalScript" and kinds[-2:] == [
        "MoveFocuserAbsolute", "RunAutofocus"]
    gate = items[0]
    assert gate["Script"] == f'"{script}" af --label="NGC 604" --filter=L'
    assert gate["ErrorBehavior"] == 1 and gate["Attempts"] == 1
    assert gate["Parent"] == {"$ref": g["NGC 604 start AF (re-entry gate)"]["$id"]}
    r = lint(seq, guided=False)
    assert not [f for f in r.findings if f.level == "ERROR"], r.findings
    v = dispatch_gate.check(seq, guided=False)
    assert v["ok"], v


def test_on_mode_gates_slew_and_center_and_keeps_tracking(monkeypatch, no_moon,
                                                          script):
    seq = _gen(monkeypatch, "on")
    g = _gated(seq)
    assert set(g) == {"NGC 604 slew (re-entry gate)",
                      "NGC 604 start AF (re-entry gate)",
                      "NGC 604 center (re-entry gate)"}
    slew = g["NGC 604 slew (re-entry gate)"]["Items"]["$values"]
    assert slew[0]["Script"].endswith('slew --label="NGC 604" --ra=1.57800 '
                                      '--dec=+30.78000')
    assert "SlewScopeToRaDec" in slew[1]["$type"]
    # mid-night dispatch: no stopped tracking before the targets, it moved
    # to the End area after the park
    start = next(d for d in _walk(seq) if d.get("Name") == "AARO startup")
    assert not [i for i in start["Items"]["$values"]
                if "SetTracking" in i["$type"] and i.get("TrackingMode") == 5]
    end = next(d for d in _walk(seq) if d.get("Name") == "AARO shutdown")
    ends = [i["$type"].split(",")[0].split(".")[-1] for i in end["Items"]["$values"]]
    assert ends.index("SetTracking") == ends.index("ParkScope") + 1
    r = lint(seq, guided=False)
    assert not [f for f in r.findings if f.level == "ERROR"], r.findings
    assert not [f for f in r.findings if f.rule == "tracking"]
    assert dispatch_gate.check(seq, guided=False)["ok"]
    # a dusk-gated night still stops tracking while it holds for dusk
    dusk = _gen(monkeypatch, "on", dusk=True)
    start = next(d for d in _walk(dusk) if d.get("Name") == "AARO startup")
    assert [i for i in start["Items"]["$values"]
            if "SetTracking" in i["$type"] and i.get("TrackingMode") == 5]


def test_guided_smart_night_lints_and_passes_the_gate(monkeypatch, no_moon,
                                                      script):
    monkeypatch.setenv("PS_RC16_AF_POLICY", "smart")
    seq = _gen(monkeypatch, "on", targets=[_ngc604(guided=True)])
    assert len(_gated(seq)) == 3
    r = lint(seq, guided=True)
    assert not [f for f in r.findings if f.level == "ERROR"], r.findings
    assert dispatch_gate.check(seq, guided=True,
                               guided_targets=["NGC 604"])["ok"]
    # the block triggers (the in-NINA safety net) are untouched
    hfr = [d for d in _walk(seq) if "AutofocusAfterHFRIncreaseTrigger"
           in str(d.get("$type"))]
    assert hfr


def test_lint_rule_catches_a_bad_gate(monkeypatch, no_moon, script):
    seq = _gen(monkeypatch, "af")
    c = _gated(seq)["NGC 604 start AF (re-entry gate)"]
    vals = c["Items"]["$values"]
    vals.append(nsj._make_typed(
        "NINA.Sequencer.SequenceItem.Imaging.TakeExposure, NINA.Sequencer",
        ExposureTime=120, ImageType="LIGHT"))
    vals[0]["ErrorBehavior"] = 0
    errs = [f.detail for f in lint(seq, guided=False).findings
            if f.rule == "reentry-gate"]
    assert any("ErrorBehavior 1" in e for e in errs)
    assert any("would also skip TakeExposure" in e for e in errs)
    vals.insert(1, vals.pop(0))       # gate not first
    errs = [f.detail for f in lint(seq, guided=False).findings
            if f.rule == "reentry-gate"]
    assert any("first and only gate" in e for e in errs)


# --- the PS-77 simulator: a SAFE_LOOP re-entry after a park -------------------------

class ReentrySim(NinaSim):
    """NinaSim + the re-entry gate: the AF gate answers SKIP when the
    previous start AF finished under `skip_min` ago (the service's time
    rule; temperature steady); SKIP interrupts the gate's Parent."""

    def __init__(self, seq, skip_min=45.0, **kw):
        super().__init__(seq, **kw)
        self.skip_s = skip_min * 60
        self.last_af = None
        self.start_afs, self.skips = [], 0

    def run_instruction(self, n):
        if n.type == "ExternalScript" and "reentry-gate" in n.d.get("Script", ""):
            if self.last_af is not None and self.t - self.last_af < self.skip_s:
                self.skips += 1
                raise _Interrupt(n.parent)
            return
        if n.type == "RunAutofocus":
            super().run_instruction(n)
            self.last_af = self.t
            if n.parent is not None and (
                    n.parent.type == "DeepSkyObjectContainer"
                    or str(n.parent.d.get("Name", "")).endswith(
                        reentry.AF_SUFFIX)):
                self.start_afs.append(self.t)
            return
        return super().run_instruction(n)


def test_sim_safe_loop_reentry_skips_the_start_af(monkeypatch, no_moon, script):
    """Roof closes for 10 min an hour into the night: the scope parks, the
    SAFE_LOOP comes back and re-enters the target. With the gate the start
    AF runs once; without it twice. Lights resume either way. (8 subs per
    pass: every_block refocuses at each pass, as NINA's triggers would.)"""
    unsafe = [(3600.0, 4200.0)]
    seq = _gen(monkeypatch, "af", targets=[_ngc604(count=8)])
    a = ReentrySim(seq, unsafe=unsafe, tick=1.0).run()
    b = ReentrySim(_gen(monkeypatch, "off", targets=[_ngc604(count=8)]),
                   unsafe=unsafe, tick=1.0).run()
    assert len(b.start_afs) == 2 and b.skips == 0
    assert len(a.start_afs) == 1 and a.skips == 1
    assert [x for x in a.lights if x["start"] > 4200]
    assert [x for x in b.lights if x["start"] > 4200]
    # an unsafe spell longer than the skip window: the AF runs again
    c = ReentrySim(seq, unsafe=[(3600.0, 3600.0 + 50 * 60)], tick=1.0).run()
    assert len(c.start_afs) == 2


# --- CLI / cmd ---------------------------------------------------------------------

def test_cli_fails_open_and_cmd_maps_only_skip():
    from typer.testing import CliRunner
    from photonscript.cli import app
    r = CliRunner().invoke(app, ["reentry-gate", "af", "--label", "X",
                                 "--url", "http://127.0.0.1:9", "--from-nina"])
    assert r.exit_code == 0 and "running the step" in r.output
    cmd = (Path(__file__).resolve().parents[2] / "deploy"
           / "reentry-gate.cmd").read_text(encoding="ascii")
    assert "reentry-gate %* --from-nina" in cmd
    assert "if %ERRORLEVEL% EQU 3 exit /b 1" in cmd and cmd.rstrip().endswith(
        "exit /b 0")
    assert reentry.EXIT_SKIP == 3


def test_config_defaults_and_fields():
    from photonscript.scheduler.app import _CONFIG_FIELDS
    c = PhotonScriptConfig()
    assert c.reentry_optimize == "advise" and reentry.mode(c) == "advise"
    assert reentry.gate_kinds(c) == {"af", "slew", "center"}   # advise emits every gate, always RUN
    assert (c.reentry_af_skip_min, c.reentry_af_temp_c, c.reentry_frozen_h,
            c.reentry_center_arcmin) == (45.0, 1.0, 6.0, 2.0)
    envs = {f[1] for f in _CONFIG_FIELDS}
    assert {"PS_REENTRY_OPTIMIZE", "PS_REENTRY_AF_SKIP_MIN",
            "PS_REENTRY_GATE_SCRIPT"} <= envs
    assert reentry.mode(PhotonScriptConfig(reentry_optimize="bogus")) == "advise"


def test_morning_report_card_carries_the_line(tmp_path):
    from photonscript.scheduler.morning_report import report_card
    cfg = _cfg(tmp_path)
    _gate(cfg, FakeIO(), filter_name="L")
    card = report_card(cfg, "2026-10-14", calibration=False,
                       completeness=False)
    assert any(str(x).startswith("Re-entry: 1 skip(s)")
               for x in card["night_pilot"])
    assert any("Re-entry: 1 skip(s)" in str(x) for x in card["lines"])
