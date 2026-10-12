"""PS-25 follow-up (2026-10-12): the Piggy-600 must not focus or shoot until
the RC16 is imaging again after EVERY unsafe pause.

2026-10-10 (NINA logs, local MDT = UTC-6): the 420 s resume hold ran on both
safe re-entries (safe 00:46:16 -> OSC image pass 00:53:21; safe 03:15:42 ->
03:22:48). The first time NINA #1 was already in its Smart Exposure (00:53:00,
first light 00:54:27). The second time NINA #1 changed target: 120 s confirm,
unpark, slew, AF, then an 8 min Center (03:19:44 to 03:27:39), its Smart
Exposure from 03:27:58 and first light 03:29:25; the OSC focused at 03:22:52
and started its first light at 03:24:45, inside the RC16's center.

The fix: a resume gate (split_guard.run_resume_gate, through the settle-gate
script with --resume) first in every OSC image pass holds until NINA #1's
deepest running item is an exposure. These tests replay both cycles: the
gate itself on the two NINA #1 timelines, and the whole companion sequence
in the PS-77 NINA simulator through two unsafe / safe cycles."""
import asyncio
import json

import pytest

from photonscript.scheduler import split_guard as sg
from photonscript.scheduler.calibration import (OSC_IMAGE_PASS_NAME,
                                                generate_piggyback_companion_json)
from photonscript.shared.config import PhotonScriptConfig

from tests.test_scheduler.test_ps77_safety_stop import NinaSim, _vals, _walk

RC16 = "http://nina1.test:1888/v2/api"


@pytest.fixture(autouse=True)
def _fresh():
    sg.LAST.clear()
    yield
    sg.LAST.clear()


@pytest.fixture
def gate_on(tmp_path, monkeypatch):
    script = tmp_path / "settle-gate.cmd"
    script.write_text("@echo off\n", encoding="ascii")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_GATE", "true")
    monkeypatch.setenv("PS_PIGGYBACK_SETTLE_SCRIPT", str(script))
    return script


def _cfg(tmp_path, **kw):
    base = dict(data_dir=tmp_path, piggyback_settle_gate=True, piggyback_enabled=True,
                piggyback_settle_poll_s=2, nina_base_url=RC16)
    base.update(kw)
    return PhotonScriptConfig(_env_file=None, **base)


# --- NINA #1's tree --------------------------------------------------------------

def _tree(leaf, unsafe=False):
    """A NINA #1 /sequence/json shape: the night loop with leaf RUNNING."""
    run = {"Name": leaf, "Status": "RUNNING"}
    if leaf is None:
        return [{"Name": "Sequence", "Status": "FINISHED", "Items": []}]
    if unsafe:
        branch = {"Name": "UNSAFE_Container", "Status": "RUNNING", "Items": [run]}
    else:
        branch = {"Name": "SAFE_LOOP_Container", "Status": "RUNNING", "Items": [
            {"Name": "Heart Nebula_Container", "Status": "RUNNING", "Items": [run]}]}
    return [{"Name": "Targets_Container", "Status": "RUNNING", "Items": [
        {"Name": "NIGHT_LOOP_Container", "Status": "RUNNING", "Items": [branch]}]}]


@pytest.mark.parametrize("leaf,unsafe,want", [
    ("Smart Exposure", False, "exposing"),
    ("Take Exposure", False, "exposing"),
    ("Center", False, "busy"),
    ("Run Autofocus", False, "busy"),
    ("Wait for Time Span", False, "busy"),
    ("Take Exposure", True, "busy"),      # roof-closed darks: not resumed
    (None, False, "idle"),
])
def test_rc16_activity(leaf, unsafe, want):
    assert sg.rc16_activity(_tree(leaf, unsafe))[0] == want


# --- the gate on the two 10-10 timelines -----------------------------------------

# NINA #1's deepest running item, seconds after the monitor read safe
CYCLE_1 = [(0, "Unpark Scope"), (7, "Slew Scope To Alt Az"), (52, "Run Autofocus"),
           (184, "Slew Scope To Ra Dec"), (246, "Run Autofocus"), (318, "Center"),
           (394, "External Script"), (404, "Smart Exposure")]        # light +491
CYCLE_2 = [(0, "Wait Until Safe"), (2, "Wait for Time Span"), (123, "Unpark Scope"),
           (142, "Slew Scope To Ra Dec"), (170, "Run Autofocus"), (242, "Center"),
           (725, "External Script"), (736, "Smart Exposure")]        # light +823
PIGGY_GATE_AT = {1: 425, 2: 426}       # end of the 420 s hold + wait safe


class _Clock:
    def __init__(self, t=0.0):
        self.t = t

    def __call__(self):
        return self.t

    async def sleep(self, s):
        self.t += s


def _at(timeline, t):
    leaf = None
    for t0, name in timeline:
        if t >= t0:
            leaf = name
    return leaf


def _replay(cfg, timeline, start):
    clock = _Clock(start)

    async def read(_c):
        return _tree(_at(timeline, clock()))
    return asyncio.run(sg.run_resume_gate(cfg, "OSC resume", read=read,
                                          sleep=clock.sleep, clock=clock)), clock()


def test_gate_replays_both_10_10_resumes(tmp_path):
    cfg = _cfg(tmp_path)
    r1, t1 = _replay(cfg, CYCLE_1, PIGGY_GATE_AT[1])
    assert r1["verdict"] == "PASS" and r1["waited_s"] == 0       # RC16 already imaging
    r2, t2 = _replay(cfg, CYCLE_2, PIGGY_GATE_AT[2])
    assert r2["verdict"] == "PASS" and "Center" in r2["held"], r2
    assert 736 <= t2 < 736 + 2.5                                 # one poll after
    # the OSC AF now starts after the RC16 is past its center, and its first
    # light (AF takes ~110 s on the OSC) after the RC16's first light (+823)
    assert t2 + 110 > 823
    ev = [json.loads(ln) for ln in (tmp_path / "runs").glob("*_events.jsonl").__next__()
          .read_text(encoding="utf-8").splitlines()]
    assert [e["kind"] for e in ev] == ["resume_gate", "resume_gate"]


def test_gate_is_bounded_and_fails_open(tmp_path):
    cfg = _cfg(tmp_path, piggyback_resume_wait_max_s=300)
    r, t = _replay(cfg, [(0, "Center")], 0)
    assert r["verdict"] == "TIMEOUT" and 300 <= t <= 302
    clock = _Clock()

    async def unreadable(_c):
        return None
    r = asyncio.run(sg.run_resume_gate(cfg, read=unreadable, sleep=clock.sleep,
                                       clock=clock))
    assert r["verdict"] == "UNKNOWN" and clock() < 10
    r, _t = _replay(cfg, [(0, None)], 0)
    assert r["verdict"] == "PASS" and "no sequence" in r["reason"]
    assert sg.run_resume_gate.__doc__ and "writes nothing" in sg.run_resume_gate.__doc__
    off = asyncio.run(sg.run_resume_gate(_cfg(tmp_path, piggyback_resume_gate=False)))
    assert off["verdict"] == "OFF"


def test_endpoint_mode_resume(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from photonscript.scheduler import app as app_mod
    monkeypatch.setattr(app_mod, "get_config", lambda: _cfg(tmp_path))
    seen = []

    async def fake_resume(cfg, label="", **kw):
        seen.append(("resume", label))
        return {"verdict": "PASS", "waited_s": 0}

    async def fake_settle(cfg, label="", **kw):
        seen.append(("settle", label))
        return {"verdict": "PASS", "waited_s": 0}
    monkeypatch.setattr(sg, "run_resume_gate", fake_resume)
    monkeypatch.setattr(sg, "run_settle_gate", fake_settle)
    c = TestClient(app_mod.app)
    assert c.post("/api/piggyback/settle-gate?label=x&mode=resume").json()["verdict"] == "PASS"
    assert c.post("/api/piggyback/settle-gate?label=y").json()["verdict"] == "PASS"
    assert seen == [("resume", "x"), ("settle", "y")]


def test_cli_resume_flag(monkeypatch):
    import io
    import urllib.request

    from typer.testing import CliRunner

    from photonscript import cli
    urls = []

    class _R(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake(req, timeout=None):
        urls.append((req.full_url, timeout))
        return _R(json.dumps({"verdict": "PASS", "waited_s": 1, "reason": "x"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", fake)
    r = CliRunner().invoke(cli.app, ["settle-gate", '--label=OSC resume', "--resume",
                                     "--from-nina"])
    assert r.exit_code == 0 and "resume gate" in r.output
    assert "mode=resume" in urls[0][0] and urls[0][1] >= 900


# --- the companion sequence ------------------------------------------------------

def _companion(**kw):
    from photonscript.shared.rigs import PIGGYBACK, rig_config
    cfg = PhotonScriptConfig(piggyback_setpoint_c=-5.0, **kw)
    return json.loads(generate_piggyback_companion_json(
        rig_config(cfg, PIGGYBACK), has_safety=True, with_lights=True))


def _short(d):
    return d.get("$type", "").split(",")[0].split(".")[-1]


def test_every_image_pass_starts_with_the_resume_gate(gate_on, monkeypatch):
    seq = _companion()
    ip = [d for d in _walk(seq) if d.get("Name") == OSC_IMAGE_PASS_NAME][0]
    first = _vals(ip, "Items")[0]
    assert _short(first) == "ExternalScript" and "--resume" in first["Script"]
    assert first["ErrorBehavior"] == 0 and first["Attempts"] == 1
    names = [_short(i) for i in _vals(ip, "Items")]
    assert names.index("ExternalScript") < names.index("RunAutofocus")
    monkeypatch.setenv("PS_PIGGYBACK_RESUME_GATE", "false")
    ip = [d for d in _walk(_companion()) if d.get("Name") == OSC_IMAGE_PASS_NAME][0]
    assert "--resume" not in json.dumps(_vals(ip, "Items"))


# Two unsafe / safe cycles (seconds after astro dusk), and when NINA #1 is in
# an exposure item again after each safe entry (the 10-10 numbers).
UNSAFE = [(7200, 9000), (14400, 20400)]
RC16_BACK = {0: 600, 9000: 9000 + 404, 20400: 20400 + 736}


class ResumeSim(NinaSim):
    """NINA #2 with the resume gate: an ExternalScript with --resume holds
    until the RC16 exposes again after the latest safe entry (RC16_BACK), at
    most max_s; the per-light settle gate passes at once (still mount)."""

    def __init__(self, seq, max_s=900.0, **kw):
        super().__init__(seq, **kw)
        self.max_s, self.af, self.gates = max_s, [], []

    def _rc16_back(self, t):
        return max(v for k, v in RC16_BACK.items() if k <= t)

    def run_instruction(self, n):
        if n.type == "ExternalScript" and "--resume" in n.d.get("Script", ""):
            until = min(self._rc16_back(self.t), self.t + self.max_s)
            self.gates.append((self.t, until))
            if until > self.t:
                self.advance(until - self.t)
            return
        if n.type == "ExternalScript":
            return
        if n.type == "RunAutofocus":
            self.af.append(self.t)
        return super().run_instruction(n)


def _after(ts, t0, t1):
    return [t for t in ts if t0 <= t < t1]


def test_sim_two_unsafe_cycles_hold_until_the_rc16_images(gate_on):
    sim = ResumeSim(_companion(), unsafe=UNSAFE).run()
    starts = [x["start"] for x in sim.lights]
    bounds = [0, 9000, 20400, 10 ** 9]
    for i, safe_t in enumerate((9000, 20400)):
        nxt = bounds[i + 2]
        af = _after(sim.af, safe_t, nxt)
        lights = _after(starts, safe_t, nxt)
        assert af and lights, f"cycle {i + 1}: no OSC AF / lights after the resume"
        back = RC16_BACK[safe_t]
        hold = 120 + 300
        # the fixed hold applies on every resume, and the gate on top of it
        assert af[0] >= safe_t + hold and lights[0] >= safe_t + hold
        assert af[0] >= back and lights[0] >= back, (i + 1, af[0], back)
    # the gate ran on every safe entry (dusk + both resumes)
    gate_cycles = {max(k for k in RC16_BACK if k <= t) for t, _u in sim.gates}
    assert gate_cycles == {0, 9000, 20400}


def test_sim_without_the_gate_shows_the_10_10_failure(gate_on, monkeypatch):
    monkeypatch.setenv("PS_PIGGYBACK_RESUME_GATE", "false")
    sim = ResumeSim(_companion(), unsafe=UNSAFE).run()
    af2 = _after(sim.af, 20400, 10 ** 9)
    assert af2 and af2[0] < RC16_BACK[20400]      # OSC AF inside the RC16 center
