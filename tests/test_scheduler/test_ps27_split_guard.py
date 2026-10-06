"""PS-27 part 2: the Piggy-600 never wastes a whole sub on a mount move.

Covers the companion's OSC light loop (each light right after the settle-gate
ExternalScript, ErrorBehavior 0), the lint rule, the PS-77 simulator (a
2026-09-21-like night of RC16 moves: the gate keeps subs from starting into
a move, the abort reclaims the ones a move lands in), the gate logic (passes
when still, waits during a move, fails open, never skips), the abort path
(talks only to NINA #2, only for an OSC light), the per-night split rate,
the CLI / cmd exit codes, config and API."""
import asyncio
import json
from pathlib import Path

import pytest

from photonscript.scheduler import split_guard as sg
from photonscript.scheduler.calibration import OSC_IMAGE_PASS_NAME, OSC_LIGHT_LOOP_NAME
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.mount_motion import MotionTracker

from tests.test_scheduler.test_ps77_safety_stop import NinaSim, _vals, _walk

REPO = Path(__file__).resolve().parents[2]
RC16 = "http://nina1.test:1888/v2/api"
NINA2 = "http://nina2.test:1889/v2/api"


@pytest.fixture
def gate_on(tmp_path, monkeypatch):
    script = tmp_path / "settle-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "true")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_SCRIPT", str(script))
    return script


@pytest.fixture(autouse=True)
def _fresh():
    sg.LAST.clear()
    sg._ABORT["at"] = None
    yield
    sg.LAST.clear()
    sg._ABORT["at"] = None


def _companion(**cfg_kw):
    from photonscript.scheduler.calibration import generate_piggyback_companion_json
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    cfg = PhotonScriptConfig(piggyback_setpoint_c=-5.0, **cfg_kw)
    return json.loads(generate_piggyback_companion_json(
        rig_config(cfg, PIGGYBACK), has_safety=True, with_lights=True))


def _loop(seq):
    return [d for d in _walk(seq) if d.get("Name") == OSC_LIGHT_LOOP_NAME][0]


def _short(d):
    return d.get("$type", "").split(",")[0].split(".")[-1]


# --- the companion sequence -----------------------------------------------------

def test_every_osc_light_waits_at_the_settle_gate(gate_on):
    seq = _companion()
    loop = _loop(seq)
    items = _vals(loop, "Items")
    # after each light (the loop's dawn check then sees the exposure next)
    assert [_short(i) for i in items] == ["TakeExposure", "ExternalScript"]
    gate = items[1]
    assert "settle-gate" in gate["Script"] and '--label="OSC 120s"' in gate["Script"]
    assert gate["ErrorBehavior"] == 0 and gate["Attempts"] == 1   # never skips
    assert gate["Parent"] == {"$ref": loop["$id"]}
    # ... and once right before the loop, for its first light
    ip = [d for d in _walk(seq) if d.get("Name") == OSC_IMAGE_PASS_NAME][0]
    names = [_short(i) if i.get("Name") != OSC_LIGHT_LOOP_NAME else "LOOP"
             for i in _vals(ip, "Items")]
    assert names[-2:] == ["ExternalScript", "LOOP"]
    assert "settle-gate" in _vals(ip, "Items")[-2]["Script"]
    assert not [f for f in lint(seq).findings
                if f.rule in ("settle-gate", "parent-links")]


def test_gate_off_is_the_old_light_loop(gate_on, monkeypatch):
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "false")
    seq = _companion()
    assert [_short(i) for i in _vals(_loop(seq), "Items")] == ["TakeExposure"]
    assert not [f for f in lint(seq).findings if f.rule == "settle-gate"]


def test_missing_script_emits_no_gate_and_says_so(monkeypatch, tmp_path):
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "true")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_SCRIPT", str(tmp_path / "gone.cmd"))
    seq = _companion()
    assert [_short(i) for i in _vals(_loop(seq), "Items")] == ["TakeExposure"]
    start = [d for d in _walk(seq) if "StartAreaContainer" in d.get("$type", "")][0]
    assert any("settle gate OFF tonight" in str(i.get("Text", "")) + str(i.get("Message", ""))
               for i in _vals(start, "Items"))
    r = lint(seq)
    assert any(f.rule == "settle-gate" and f.level == "WARN"
                        for f in r.findings)


# --- the lint rule -----------------------------------------------------------------

def test_lint_flags_an_ungated_osc_light(gate_on):
    seq = _companion()
    _loop(seq)["Items"]["$values"].pop(1)
    errs = [f for f in lint(seq).findings if f.rule == "settle-gate"]
    assert errs and errs[0].level == "ERROR" and errs[0].detail.startswith("1 place")


def test_lint_flags_a_loop_entered_without_the_gate(gate_on):
    seq = _companion()
    ip = [d for d in _walk(seq) if d.get("Name") == OSC_IMAGE_PASS_NAME][0]
    ip["Items"]["$values"].pop(-2)
    errs = [f for f in lint(seq).findings if f.rule == "settle-gate"]
    assert errs and errs[0].detail.startswith("1 place")


def test_lint_flags_a_gate_that_could_skip(gate_on):
    seq = _companion()
    _vals(_loop(seq), "Items")[1]["ErrorBehavior"] = 1
    assert any(f.rule == "settle-gate" and "never skip" in f.detail
               for f in lint(seq).findings)


def test_lint_rule_ignores_rc16_sequences():
    from photonscript.scheduler import nina_sequence_json as nsj
    from photonscript.scheduler.nina_sequence import build_sequence_for_night
    from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget
    t = NinaSequenceTarget(name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                           exposures=[ExposurePlan(filter_type=FilterType.HA,
                                                   exposure_seconds=300, count=10,
                                                   gain=200, offset=256)])
    seq = json.loads(nsj.generate_nina_json(build_sequence_for_night("PS27", [t])))
    assert not [f for f in lint(seq, settle_gate=True).findings
                if f.rule == "settle-gate"]


def test_lint_rule_can_be_forced_without_config():
    seq = _companion()                                   # conftest: gate off
    assert any(f.rule == "settle-gate" and f.level == "ERROR"
               for f in lint(seq, settle_gate=True).findings)


# --- the PS-77 simulator: a night of RC16 moves --------------------------------------

def _moves(every_s=480, dur_s=40, start=1800, end=8 * 3600):
    """2026-09-21: one slew / AF / center / guide loop about every 8 min."""
    return [(t, t + dur_s) for t in range(start, end, every_s)]


class SplitSim(NinaSim):
    """NinaSim for NINA #2 plus the shared mount: `moves` are the RC16's
    (start, end) mount moves. The settle gate holds while a move runs or
    ended less than still_s ago (at most timeout_s). With abort, a light a
    move starts in ends `detect_s` later, unsaved (ErrorBehavior 0: the loop
    goes on to the gate and a fresh sub)."""

    def __init__(self, seq, moves, abort=False, still_s=6, timeout_s=90,
                 detect_s=5, **kw):
        super().__init__(seq, **kw)
        self.moves, self.abort = moves, abort
        self.still_s, self.timeout_s, self.detect_s = still_s, timeout_s, detect_s
        self.aborted, self.gate_holds = [], 0

    def _busy_until(self, t):
        for a, b in self.moves:
            if a <= t < b + self.still_s:
                return b + self.still_s
        return None

    def run_instruction(self, n):
        if n.type == "ExternalScript" and "settle-gate" in n.d.get("Script", ""):
            t_end = self.t + self.timeout_s
            until = self._busy_until(self.t)
            while until is not None and self.t < t_end:
                self.gate_holds += 1
                self.advance(min(until, t_end) - self.t)
                until = self._busy_until(self.t)
            return
        if (self.abort and n.type == "TakeExposure"
                and str(n.d.get("ImageType", "")).upper() == "LIGHT"):
            start, dur = self.t, self.duration(n)
            hit = [a for a, b in self.moves if start < a < start + dur]
            if hit:
                self.advance(hit[0] + self.detect_s - start)
                self.aborted.append({"start": start, "end": self.t})
                return
        return super().run_instruction(n)

    def splits(self):
        return [x for x in self.lights if x["done"] and any(
            x["start"] < b and a < x["end"] for a, b in self.moves)]


def test_sim_still_mount_shoots_exactly_as_before(gate_on, monkeypatch):
    a = SplitSim(_companion(), moves=[]).run()
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "false")
    b = NinaSim(_companion()).run()
    assert a.lights and a.gate_holds == 0
    assert [(x["start"], x["end"]) for x in a.lights] == \
        [(x["start"], x["end"]) for x in b.lights]


def test_sim_gate_never_starts_a_sub_into_a_move(gate_on, monkeypatch):
    moves = _moves()
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "false")
    before = SplitSim(_companion(), moves).run()
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "true")
    gated = SplitSim(_companion(), moves).run()
    assert len(before.splits()) >= 40          # about every move splits a sub
    assert gated.gate_holds > 0
    # what is left are subs a move landed in after they started: no sub
    # starts during a move or its settle
    assert not [x for x in gated.lights if gated._busy_until(x["start"])]
    assert len(gated.splits()) <= len(before.splits())


def test_sim_gate_plus_abort_leaves_no_split_subs(gate_on):
    moves = _moves()
    sim = SplitSim(_companion(), moves, abort=True).run()
    assert sim.splits() == []
    assert sim.aborted and len(sim.lights) > 100
    # every aborted sub ended within detect_s of the move that hit it
    assert all(any(0 < x["end"] - a <= 5 for a, _ in moves) for x in sim.aborted)


def test_sim_gate_holds_are_bounded(gate_on):
    """A mount that never settles (a long flip) delays each sub by at most the
    timeout, then the loop shoots anyway: the gate never wedges NINA #2."""
    sim = SplitSim(_companion(), moves=[(3000, 3000 + 1800)]).run()
    inside = [x for x in sim.lights if 3000 < x["start"] < 4800]
    assert inside                                  # timed out, kept shooting
    gaps = [b["start"] - a["end"] for a, b in zip(sim.lights, sim.lights[1:])]
    assert max(gaps) <= 90 + 1


# --- the gate itself ------------------------------------------------------------------

class _Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def _mount(ra_h=2.5, dec=61.5, slewing=False, parked=False, pier="pierEast"):
    return {"RightAscension": ra_h, "Declination": dec, "Slewing": slewing,
            "AtPark": parked, "SideOfPier": pier, "TrackingEnabled": True}


def _cfg(tmp_path, **kw):
    base = dict(data_dir=tmp_path, piggyback_settle_gate=True,
                piggyback_settle_timeout_s=90, piggyback_settle_still_s=6,
                piggyback_settle_poll_s=2, piggyback_enabled=True,
                nina_base_url=RC16, piggyback_nina_base_url=NINA2)
    base.update(kw)
    return PhotonScriptConfig(_env_file=None, **base)


def _gate(cfg, mounts, tracker=None, clock=None, **kw):
    clock = clock or _Clock()
    seq = list(mounts)
    reads = []

    async def read(_cfg):
        reads.append(clock())
        return seq.pop(0) if len(seq) > 1 else seq[0]

    res = asyncio.run(sg.run_settle_gate(cfg, "OSC 120s", read=read,
                                         sleep=clock.sleep, clock=clock,
                                         tracker=tracker or MotionTracker(), **kw))
    return res, reads


def _events(cfg):
    from photonscript.shared.phd2_store import read_jsonl
    return [e for f in sorted((Path(cfg.data_dir) / "runs").glob("*_events.jsonl"))
            for e in read_jsonl(f)]


def _agent_polls(tr, until=1000.0, n=12):
    """The RC16 agent's 5 s mount poll for the last minute."""
    for k in range(n, 0, -1):
        tr.observe({"ra": 37.5, "dec": 61.5, "slewing": False, "parked": False,
                    "pier": "East"}, until - 5 * k)


def test_gate_passes_at_once_on_a_mount_that_has_been_still(tmp_path):
    cfg = _cfg(tmp_path)
    tr = MotionTracker()
    _agent_polls(tr)
    res, reads = _gate(cfg, [_mount()], tracker=tr)
    assert res["verdict"] == "PASS" and res["waited_s"] == 0 and len(reads) == 1
    assert _events(cfg) == []                        # no-wait pass: not logged


def test_gate_watches_still_s_when_it_has_no_history(tmp_path):
    res, reads = _gate(_cfg(tmp_path), [_mount()])
    assert res["verdict"] == "PASS" and res["waited_s"] == 6
    assert res["held"] == ["watching"]


def test_gate_waits_through_a_slew_then_the_settle(tmp_path):
    cfg = _cfg(tmp_path)
    m = [_mount()] + [_mount(slewing=True)] * 5 + [_mount(ra_h=2.6)]
    res, _ = _gate(cfg, m)
    assert res["verdict"] == "PASS"
    assert "slewing" in res["held"] and "moved" in res["held"]
    # polls every 2 s: slewing at 2..10 s, slew-end seen at 12 s, then
    # still_s (6 s) after that
    assert res["waited_s"] == 18
    ev = _events(cfg)
    assert ev and ev[-1]["kind"] == "settle_gate" and ev[-1]["rig"] == "piggyback"
    assert ev[-1]["value"] == "PASS" and "slewing" in ev[-1]["held"]


def test_gate_waits_for_a_phd2_settle(tmp_path):
    clock = _Clock()
    tr = MotionTracker()
    _agent_polls(tr, clock())
    tr.observe_guider(True, clock() - 2)
    cfg = _cfg(tmp_path)
    orig = clock.sleep

    async def sleep(s):
        await orig(s)
        if clock() >= 1010:
            tr.observe_guider(False, clock())
    clock.sleep = sleep
    res, _ = _gate(cfg, [_mount()], tracker=tr, clock=clock)
    assert res["verdict"] == "PASS" and res["held"] == ["settling"]
    assert 10 <= res["waited_s"] <= 12


def test_gate_times_out_and_shoots_anyway(tmp_path):
    cfg = _cfg(tmp_path)
    res, _ = _gate(cfg, [_mount(slewing=True)])
    assert res["verdict"] == "TIMEOUT" and res["waited_s"] == 90
    assert _events(cfg)[-1]["value"] == "TIMEOUT"


def test_gate_fails_open_when_nina1_is_unreadable(tmp_path):
    res, reads = _gate(_cfg(tmp_path), [None])
    assert res["verdict"] == "UNKNOWN" and len(reads) == 3 and res["waited_s"] == 4


def test_gate_off_does_not_read(tmp_path):
    res, reads = _gate(_cfg(tmp_path, piggyback_settle_gate=False), [_mount()])
    assert res["verdict"] == "OFF" and reads == []


def test_gate_stops_quietly_when_nina_cancels(tmp_path):
    async def gone():
        return True
    res, reads = _gate(_cfg(tmp_path), [_mount(slewing=True)], disconnected=gone)
    assert res["verdict"] == "ABORTED" and reads == []


def test_gate_only_reads_nina1(tmp_path, monkeypatch):
    urls = []

    async def fake_get(url, timeout=5.0):
        urls.append(url)
        return _mount()
    monkeypatch.setattr(sg, "_http_get", fake_get)
    clock = _Clock()
    res = asyncio.run(sg.run_settle_gate(_cfg(tmp_path), sleep=clock.sleep,
                                         clock=clock, tracker=MotionTracker()))
    assert res["verdict"] == "PASS"
    assert urls and all(u == RC16 + "/equipment/mount/info" for u in urls)


def test_tracker_move_kinds_and_staleness():
    tr = MotionTracker()
    s = {"ra": 10.0, "dec": 20.0, "slewing": False, "parked": False, "pier": "East"}
    assert tr.observe(s, 0) is None and tr.still_s(5) == 5
    assert tr.observe({**s, "ra": 10.001}, 5) is None          # a dither
    assert tr.observe({**s, "slewing": True}, 10) == "slew-start"
    assert tr.observe({**s, "slewing": True}, 15) == "slewing"
    assert tr.observe({**s, "ra": 11.0}, 20) == "slew-end"
    assert tr.observe({**s, "ra": 11.5}, 25) == "move"
    assert tr.observe({**s, "ra": 11.5, "pier": "West"}, 30) == "pier"
    assert tr.still_s(36) == 6
    assert tr.still_s(36 + 31) is None                          # poll stopped
    tr.observe_guider(True, 40)
    assert tr.guider_settling(50) and not tr.guider_settling(40 + 181)


# --- abort on move ----------------------------------------------------------------------

def _tree(running_leaf="Take Exposure", trigger_running=False, loop_running=True):
    st = "RUNNING" if loop_running else "FINISHED"
    return [{"Name": "Targets_Container", "Status": "RUNNING", "Items": [
        {"Name": "OSC_IMAGE_PASS_Container", "Status": "RUNNING", "Items": [
            {"Name": OSC_LIGHT_LOOP_NAME + "_Container", "Status": st,
             "Triggers": [{"Name": "AutofocusAfterHFRIncrease",
                           "Status": "RUNNING" if trigger_running else "CREATED"}],
             "Items": [{"Name": "External Script", "Status": "FINISHED"},
                       {"Name": running_leaf, "Status": "RUNNING"}]}]}]}]


def _fake_nina2(tree=None, exposing=True):
    calls = []

    async def get(url, timeout=5.0):
        calls.append(url)
        if url.endswith("/sequence/json"):
            return tree if tree is not None else _tree()
        if url.endswith("/equipment/camera/info"):
            return {"IsExposing": exposing}
        if url.endswith("/equipment/camera/abort-exposure"):
            return "Exposure aborted"
        raise AssertionError(url)
    return get, calls


def _abort(cfg, mounts, get, t0=1000.0):
    tr = MotionTracker()
    out = []
    for i, m in enumerate(mounts):
        out.append(asyncio.run(sg.on_rc16_mount(cfg, m, now=t0 + 5 * i,
                                                tracker=tr, get=get)))
    return out


def test_abort_on_slew_talks_only_to_nina2(tmp_path):
    cfg = _cfg(tmp_path, piggyback_abort_on_move=True)
    get, calls = _fake_nina2()
    res = _abort(cfg, [_mount(), _mount(slewing=True)], get)
    assert res[0] is None and res[1]["aborted"] is True
    assert calls == [NINA2 + "/sequence/json", NINA2 + "/equipment/camera/info",
                     NINA2 + "/equipment/camera/abort-exposure"]
    assert not any(u.startswith(RC16) for u in calls)
    ev = _events(cfg)
    assert ev[-1]["kind"] == "split_abort" and ev[-1]["value"] == "slew-start"


def test_abort_once_per_move_episode(tmp_path):
    cfg = _cfg(tmp_path, piggyback_abort_on_move=True)
    get, calls = _fake_nina2()
    res = _abort(cfg, [_mount(), _mount(slewing=True), _mount(slewing=True),
                       _mount(slewing=True)], get)
    assert [bool(r and r["aborted"]) for r in res] == [False, True, False, False]
    assert sum(u.endswith("abort-exposure") for u in calls) == 1


@pytest.mark.parametrize("tree,exposing", [
    (_tree(trigger_running=True), True),          # PS-68 refocus frame
    (_tree(running_leaf="External Script"), True),  # waiting at the gate
    (_tree(loop_running=False), True),            # darks / flats / hold
    (_tree(), False),                             # downloading, not exposing
])
def test_no_abort_unless_an_osc_light_is_exposing(tmp_path, tree, exposing):
    cfg = _cfg(tmp_path, piggyback_abort_on_move=True)
    get, calls = _fake_nina2(tree, exposing)
    res = _abort(cfg, [_mount(), _mount(slewing=True)], get)
    assert res[1]["aborted"] is False
    assert not any(u.endswith("abort-exposure") for u in calls)


def test_dither_and_abort_off_never_abort(tmp_path):
    get, calls = _fake_nina2()
    on = _cfg(tmp_path, piggyback_abort_on_move=True)
    assert _abort(on, [_mount(), _mount(ra_h=2.5 + 0.2 / 3600 / 15 * 60)], get) == [None, None]
    off = _cfg(tmp_path)                             # default: off
    assert off.piggyback_abort_on_move is False
    assert _abort(off, [_mount(), _mount(slewing=True)], get) == [None, None]
    assert calls == []


def test_abort_on_a_jump_and_a_flip(tmp_path):
    cfg = _cfg(tmp_path, piggyback_abort_on_move=True)
    get, _ = _fake_nina2()
    r = _abort(cfg, [_mount(), _mount(ra_h=2.52)], get)        # ~18' jump
    assert r[1]["aborted"] and r[1]["kind"] == "move"
    sg._ABORT["at"] = None
    r = _abort(cfg, [_mount(), _mount(pier="pierWest")], get)
    assert r[1]["aborted"] and r[1]["kind"] == "pier"


def test_abort_fails_quietly_when_nina2_is_down(tmp_path):
    cfg = _cfg(tmp_path, piggyback_abort_on_move=True)

    async def down(url, timeout=5.0):
        raise OSError("refused")
    r = _abort(cfg, [_mount(), _mount(slewing=True)], down)
    assert r[1]["aborted"] is False and "unreachable" in r[1]["reason"]


# --- per-night split rate -----------------------------------------------------------------

def test_night_split_summary(tmp_path):
    from photonscript.shared.phd2_store import append_jsonl
    cfg = _cfg(tmp_path)
    subs = ([{"rig": "piggyback", "slew_overlap_s": 0.0}] * 37
            + [{"rig": "piggyback", "slew_overlap_s": 42.0}] * 2
            + [{"rig": "piggyback", "slew_overlap_s": None}]
            + [{"rig": "rc16", "slew_overlap_s": 50.0}] * 5)
    ev = Path(tmp_path) / "runs" / "2026-10-06_events.jsonl"
    for line in ([{"src": "photonscript", "kind": "split_abort", "value": "slew-start"}]
                 + [{"src": "photonscript", "kind": "settle_gate", "value": "PASS",
                     "held": ["slewing", "moved"]}] * 3
                 + [{"src": "photonscript", "kind": "settle_gate", "value": "TIMEOUT",
                     "held": ["slewing"]}]
                 + [{"src": "photonscript", "kind": "settle_gate", "value": "PASS",
                     "held": ["watching"]}]
                 + [{"src": "nina", "kind": "instruction", "value": "x"}]):
        append_jsonl(ev, line)
    s = sg.night_split_summary(cfg, "2026-10-06", subs=subs)
    assert (s["lights"], s["judged"], s["straddled"], s["aborted"]) == (40, 39, 2, 1)
    assert s["attempted"] == 40 and s["rate"] == 0.05 and s["passed"] is False
    assert (s["holds"], s["saves"], s["timeouts"]) == (5, 4, 1)
    empty = sg.night_split_summary(cfg, "2026-10-07", subs=[])
    assert empty["rate"] is None and empty["passed"] is None


# --- CLI, cmd wrapper, API, config ----------------------------------------------------------

def _fake_urlopen(monkeypatch, body=None, exc=None):
    import io
    import urllib.request

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        if exc:
            raise exc
        assert "/api/piggyback/settle-gate?" in req.full_url
        return _R(json.dumps(body).encode())

    monkeypatch.setattr(urllib.request, "urlopen", fake)


@pytest.mark.parametrize("verdict", ["PASS", "TIMEOUT", "UNKNOWN", "OFF"])
def test_cli_always_exits_0(monkeypatch, verdict):
    from typer.testing import CliRunner

    from photonscript import cli
    _fake_urlopen(monkeypatch, {"verdict": verdict, "waited_s": 3, "reason": "x"})
    r = CliRunner().invoke(cli.app, ["settle-gate", '--label=OSC 120s', "--from-nina"])
    assert r.exit_code == 0 and verdict in r.output


def test_cli_fails_open_when_the_service_is_down(monkeypatch):
    from typer.testing import CliRunner

    from photonscript import cli
    _fake_urlopen(monkeypatch, exc=OSError("refused"))
    r = CliRunner().invoke(cli.app, ["settle-gate", "--from-nina"])
    assert r.exit_code == 0 and "shooting anyway" in r.output


def test_cmd_wrapper_always_exits_0():
    text = (REPO / "deploy" / "settle-gate.cmd").read_bytes()
    assert all(b < 128 for b in text)
    s = text.decode("ascii")
    assert "settle-gate %* --from-nina" in s
    assert "exit /b 1" not in s and s.rstrip().endswith("exit /b 0")
    assert PhotonScriptConfig(_env_file=None).piggyback_settle_script.endswith(
        "deploy\\settle-gate.cmd")


def test_config_defaults_and_system_fields(monkeypatch):
    from photonscript.scheduler.app import _CONFIG_FIELDS
    monkeypatch.delenv("PS_PIGGYBACK_SETTLE_GATE", raising=False)   # conftest
    c = PhotonScriptConfig(_env_file=None)
    assert c.piggyback_settle_gate is True and c.piggyback_abort_on_move is False
    assert (c.piggyback_settle_timeout_s, c.piggyback_settle_still_s,
            c.piggyback_abort_move_arcmin) == (90.0, 6.0, 0.5)
    by_attr = {f[0]: f for f in _CONFIG_FIELDS}
    for k in ("piggyback_settle_gate", "piggyback_settle_timeout_s",
              "piggyback_settle_still_s", "piggyback_settle_script",
              "piggyback_abort_on_move", "piggyback_abort_move_arcmin"):
        assert by_attr[k][1] == "PS_" + k.upper() and by_attr[k][3] == "Piggyback"


def test_api_status_and_run_split(tmp_path, monkeypatch):
    from photonscript.scheduler import app
    from photonscript.scheduler.routers import split_guard as r
    monkeypatch.setattr(app, "_config", _cfg(tmp_path))
    s = r.api_split_guard_status()
    assert s["settle_gate"] is True and s["abort_on_move"] is False
    assert s["script_found"] is False and "motion" in s and "tonight" in s
    assert r.api_run_split("2026-10-06")["attempted"] == 0
    paths = {getattr(x, "path", "") for x in r.router.routes}
    assert {"/api/piggyback/settle-gate", "/api/piggyback/split-guard",
            "/api/runs/{date}/split"} <= paths
    assert app._split_guard_router is r                  # mounted in app.py


# --- the RC16 agent hooks -------------------------------------------------------------------

def test_rc16_agent_poll_feeds_the_split_guard(tmp_path, monkeypatch):
    from photonscript.telescope_agent import agent as agent_mod
    from tests.test_scheduler.test_ps121_mount_card import _Nina
    from tests.test_scheduler.test_ps121_mount_card import _cfg as _mcfg
    seen = []

    async def fake(cfg, mount, **kw):
        seen.append(mount)
    monkeypatch.setattr(sg, "on_rc16_mount", fake)
    ag = agent_mod.TelescopeAgent(_mcfg(tmp_path, observatory_tz="UTC"), rig="rc16")
    ag.nina = _Nina(True)
    for name in ("_cooling_watchdog", "_dew_heater_watchdog"):
        async def _noop(*a, **k):
            return None
        monkeypatch.setattr(ag, name, _noop)

    async def _stop(_s):
        ag._running = False
    monkeypatch.setattr(agent_mod.asyncio, "sleep", _stop)
    ag._running = True
    asyncio.run(ag._nina_poll_loop())
    assert len(seen) == 1 and "RightAscension" in seen[0]


def test_rc16_agent_feeds_phd2_settling(tmp_path, monkeypatch):
    from photonscript.shared import mount_motion
    from photonscript.shared.models import GuidingMetrics
    from photonscript.telescope_agent.agent import TelescopeAgent
    tr = MotionTracker()
    monkeypatch.setattr(mount_motion, "TRACKER", tr)
    ag = TelescopeAgent(_cfg(tmp_path), rig="rc16")
    ag.phd2._settling = True
    asyncio.run(ag._on_guiding_update(GuidingMetrics()))
    assert tr.settling is True
    ag.phd2._settling = False
    asyncio.run(ag._on_guiding_update(GuidingMetrics()))
    assert tr.settling is False
