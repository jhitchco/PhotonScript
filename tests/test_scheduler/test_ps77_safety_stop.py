"""PS-77: the RC16 kept imaging 38 min after the roof closed (2026-09-26).

Root cause: the generated NINA JSON carried no "$id"/"Parent" references, and
NINA sets an entity's Parent ONLY from that reference. With Parent == null,
SequentialStrategy.CanContinue never reaches the Safety / Altitude / dawn
TimeCondition on the containers above a SmartExposure, and the Safety (5 s)
and Time (1 s) condition watchdogs never interrupt a running exposure.

These tests pin the fix three ways:
  1. structure: every entity is parent-linked, every light loop guards itself
     (Safety + loop-end TimeCondition on the SmartExposure), refocus triggers
     carry the block's AF-filter + offset recipe;
  2. behavior: a small interpreter of NINA's SequentialStrategy / condition /
     watchdog rules (modeled on isbeorn/nina master: SequentialStrategy.cs,
     SequenceContainer.cs, SafetyMonitorCondition.cs, TimeCondition.cs,
     SmartExposure.cs) replays the 2026-09-26 night on the old and the new
     sequence;
  3. the armer's defense in depth: unsafe + SAFE_LOOP still running ->
     stop, stop guiding, park (cooler kept on) -> re-dispatch when safe.
"""

import copy
import json
from datetime import datetime, timedelta

import pytest

from photonscript.scheduler import nina_sequence_json as nsj
from photonscript.scheduler.nina_sequence import build_sequence_for_night
from photonscript.scheduler.sequence_lint import lint
from photonscript.shared.config import PhotonScriptConfig
from photonscript.shared.models import ExposurePlan, FilterType, NinaSequenceTarget


# --- helpers -------------------------------------------------------------------

def _short(t: str) -> str:
    return t.split(",")[0].split(".")[-1]


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _vals(node, key):
    return [x for x in (node.get(key) or {}).get("$values", []) or []
            if isinstance(x, dict)]


def _heart(oiii=40, ha=20, exp=300):
    t = NinaSequenceTarget(name="Heart Nebula", ra_hours=2.55, dec_degrees=61.5,
                           exposures=[
                               ExposurePlan(filter_type=FilterType.OIII,
                                            exposure_seconds=exp, count=oiii,
                                            gain=200, offset=256),
                               ExposurePlan(filter_type=FilterType.HA,
                                            exposure_seconds=exp, count=ha,
                                            gain=200, offset=256)])
    t.start_guiding = True
    return t


def _gen(targets=None, **kw):
    seq = build_sequence_for_night("PS77", targets or [_heart(**kw)])
    return json.loads(nsj.generate_nina_json(seq))


def _smart_exposures(seq):
    return [d for d in _walk(seq) if _short(d.get("$type", "")) == "SmartExposure"]


def _strip_links(seq):
    """What NINA saw before PS-77: no $id, no Parent anywhere."""
    out = copy.deepcopy(seq)
    for d in _walk(out):
        d.pop("$id", None)
        d.pop("Parent", None)
    out["Parent"] = None
    return out


def _as_deployed(seq):
    """origin/main (23f9f4b) equivalent: no links, and no guards on the light
    loop itself (Safety/Time lived only on its ancestors)."""
    out = _strip_links(seq)
    for d in _walk(out):
        ty = _short(d.get("$type", ""))
        if ty == "SmartExposure" or str(d.get("Name", "")).endswith(
                "imaging (repeats while safe and up)"):
            conds = d["Conditions"]["$values"]
            keep = [c for c in conds if _short(c["$type"]) not in
                    (("SafetyMonitorCondition", "TimeCondition")
                     if ty == "SmartExposure" else ("TimeCondition",))]
            d["Conditions"]["$values"] = keep
    return out


# --- 1. structure ------------------------------------------------------------------

def test_every_entity_is_parent_linked_with_leading_id():
    seq = _gen()
    ids = set()

    def visit(node, parent_id):
        assert next(iter(node)) == "$id", node.get("Name")
        assert node["$id"] not in ids
        ids.add(node["$id"])
        if parent_id is None:
            assert node.get("Parent") is None
        else:
            assert node["Parent"] == {"$ref": parent_id}, node.get("Name")
        for key in ("Items", "Conditions", "Triggers"):
            for ch in _vals(node, key):
                visit(ch, node["$id"])
        if isinstance(node.get("TriggerRunner"), dict):
            visit(node["TriggerRunner"], None)

    visit(seq, None)
    assert len(ids) > 100


def test_link_parents_does_not_touch_non_entities_or_input():
    root = nsj._seq_container("R", [nsj._center(_heart())])
    before = json.dumps(root)
    out = nsj.link_parents(root)
    assert json.dumps(root) == before                 # input untouched
    center = out["Items"]["$values"][0]
    assert center["Parent"] == {"$ref": out["$id"]}
    coords = center["Coordinates"]
    assert "$id" not in coords and "Parent" not in coords


def test_light_smart_exposures_guard_themselves():
    for se in _smart_exposures(_gen()):
        conds = [_short(c["$type"]) for c in _vals(se, "Conditions")]
        assert conds[0] == "LoopCondition"            # GetLoopCondition()
        assert "SafetyMonitorCondition" in conds
        tc = [c for c in _vals(se, "Conditions")
              if _short(c["$type"]) == "TimeCondition"]
        assert tc and tc[0]["MinutesOffset"] == -10   # all-NB: nautical dawn-10
        assert "NauticalDawnProvider" in tc[0]["SelectedProvider"]["$type"]
        trig = [_short(t["$type"]) for t in _vals(se, "Triggers")]
        assert trig[0] == "DitherAfterExposures"      # GetDitherAfterExposures()


def test_refocus_triggers_ride_the_block_with_af_filter_and_offset():
    seq = _gen()
    dso = [d for d in _walk(seq) if "DeepSkyObjectContainer" in d.get("$type", "")][0]
    dso_trig = {_short(t["$type"]) for t in _vals(dso, "Triggers")}
    assert "MeridianFlipTrigger" in dso_trig
    assert not any("Autofocus" in t for t in dso_trig)   # would AF through 3 nm
    cfg = PhotonScriptConfig()
    af = nsj._af_filter_type(cfg)
    for se in _smart_exposures(seq):
        afs = [t for t in _vals(se, "Triggers") if "Autofocus" in t["$type"]]
        assert {_short(t["$type"]) for t in afs} == {
            "AutofocusAfterTemperatureChangeTrigger",
            "AutofocusAfterHFRIncreaseTrigger"}
        for t in afs:
            steps = _vals(t["TriggerRunner"], "Items")
            kinds = [_short(s["$type"]) for s in steps]
            assert kinds[:2] == ["SwitchFilter", "RunAutofocus"]
            if af is not None:
                assert steps[0]["Filter"]["_name"] == nsj._nina_filter_name(af)


def test_inner_imaging_container_also_stops_at_loop_end():
    seq = _gen()
    inner = [d for d in _walk(seq)
             if str(d.get("Name", "")).endswith("(repeats while safe and up)")][0]
    kinds = [_short(c["$type"]) for c in _vals(inner, "Conditions")]
    assert kinds == ["SafetyMonitorCondition", "AltitudeCondition", "TimeCondition"]


def test_generated_sequence_lints_clean_and_deployed_shape_fails():
    seq = _gen()
    assert lint(seq, guided=True).ok
    old = lint(_as_deployed(seq), guided=True)
    rules = {f.rule for f in old.findings if f.level == "ERROR"}
    assert {"parent-links", "light-loop-safety", "light-loop-end"} <= rules


def test_lint_flags_a_single_broken_parent_link():
    seq = _gen()
    se = _smart_exposures(seq)[0]
    se["Conditions"]["$values"][1]["Parent"] = {"$ref": "999999"}
    res = lint(seq, guided=True)
    assert any(f.rule == "parent-links" for f in res.findings)


def test_calibration_and_companion_sequences_are_linked_too():
    from photonscript.scheduler import calibration as cal
    cfg = PhotonScriptConfig()
    outs = [json.loads(cal.generate_piggyback_companion_json(
                cfg, has_safety=True, with_lights=True)),
            json.loads(cal.generate_darks_json(cfg, [(300.0, 2)])[0]),
            json.loads(cal.generate_dusk_flats_json(cfg)[0])]
    for seq in outs:
        res = lint(seq)
        assert not [f for f in res.findings if f.rule == "parent-links"], \
            [f.detail for f in res.findings if f.rule == "parent-links"]
    # the OSC light loop already guarded itself (it stopped on 09-26)
    comp = lint(outs[0])
    assert not [f for f in comp.findings if f.rule.startswith("light-loop")]


# --- 2. behavior: a NINA SequentialStrategy interpreter -----------------------

class _Interrupt(Exception):
    def __init__(self, target):
        super().__init__(target)
        self.target = target


class _End(Exception):
    pass


class _Node:
    def __init__(self, d):
        self.d = d
        self.type = _short(d.get("$type", ""))
        self.parent = None
        self.items, self.conds = [], []
        self.status = "CREATED"
        self.iterations = 0
        self.completed = 0
        self.is_container = isinstance(d.get("Items"), dict)


# seconds relative to astro dusk
TIMES = {"NauticalDuskProvider": -1800, "DuskProvider": 0,
         "DawnProvider": 9 * 3600, "NauticalDawnProvider": 9 * 3600 + 1800}
LOOP_END = TIMES["NauticalDawnProvider"] - 600       # all-NB: nautical dawn - 10
HORIZON = TIMES["NauticalDawnProvider"] + 3 * 3600


class NinaSim:
    """Just enough of NINA 3.x to replay a night:
    - Parent comes ONLY from the JSON $ref (SequenceJsonConverter);
    - CanContinue = own conditions (or Iterations < 1) AND the Parent's, recursively;
    - FinishBlock bumps Iterations and LoopCondition.CompletedIterations,
      and a container resets its items only if it can continue;
    - Safety/LoopWhileUnsafe (5 s) and Time watchdogs interrupt cond.Parent
      only when Parent is set, reaches the root and is running; a SmartExposure
      reroutes an interrupt to its own Parent;
    - TimeCondition.Check counts the next item's estimated duration."""

    def __init__(self, seq, unsafe=()):
        self.unsafe = sorted(unsafe)
        self.t = TIMES["DuskProvider"] - 3600.0
        self.lights, self.darks, self.stack = [], 0, []
        self.steps = 0
        ids = {}

        def build(d):
            n = _Node(d)
            if "$id" in d:
                ids[d["$id"]] = n
            n.items = [build(c) for c in _vals(d, "Items")]
            n.conds = [build(c) for c in _vals(d, "Conditions")]
            return n

        self.root = build(seq)

        def link(n):
            ref = n.d.get("Parent")
            n.parent = ids.get(ref.get("$ref")) if isinstance(ref, dict) else None
            for c in n.items + n.conds:
                link(c)

        link(self.root)

    # environment
    def safe(self, t):
        return not any(a <= t < b for a, b in self.unsafe)

    def _first_unsafe(self, t0, t1):
        c = [max(a, t0) for a, b in self.unsafe if a <= t1 and b > t0]
        return min(c) if c else None

    def _first_safe(self, t0, t1):
        if self.safe(t0):
            return t0
        for a, b in self.unsafe:
            if a <= t0 < b:
                return b if b <= t1 else None
        return None

    def _next_safe(self, t):
        if self.safe(t):
            return t
        for a, b in self.unsafe:
            if a <= t < b:
                return b
        return t

    def ptime(self, cond):
        return (TIMES[_short(cond.d["SelectedProvider"]["$type"])]
                + 60 * cond.d.get("MinutesOffset", 0))

    # conditions
    def duration(self, n):
        if n is None:
            return 0.0
        if n.type == "TakeExposure":
            return float(n.d.get("ExposureTime", 0))
        if n.type == "SmartExposure":
            return self.duration(n.items[1])
        return 0.0

    def check(self, c, nxt):
        if c.type == "LoopCondition":
            return c.completed < c.d["Iterations"]
        if c.type == "SafetyMonitorCondition":
            return self.safe(self.t)
        if c.type == "LoopWhileUnsafe":
            return not self.safe(self.t)
        if c.type == "TimeCondition":
            return self.t + self.duration(nxt) <= self.ptime(c)
        return True                                   # Altitude: target up

    def can_continue(self, c, nxt):
        ok = all([self.check(x, nxt) for x in c.conds]) if c.conds \
            else c.iterations < 1
        if c.parent is not None:
            ok = ok and self.can_continue(c.parent, nxt)
        return ok

    def _in_root(self, n):
        while n is not None:
            if n.type == "SequenceRootContainer":
                return True
            n = n.parent
        return False

    def _interrupt_at(self, t0, t1):
        best = None
        for c in self.stack:
            for cond in c.conds:
                p = cond.parent
                if p is None or p not in self.stack or not self._in_root(p):
                    continue
                if cond.type == "SafetyMonitorCondition":
                    f = self._first_unsafe(t0, t1)
                    f = None if f is None else f + 5
                elif cond.type == "LoopWhileUnsafe":
                    f = self._first_safe(t0, t1)
                    f = None if f is None else f + 5
                elif cond.type == "TimeCondition":
                    f = max(t0, self.ptime(cond)) + 1
                else:
                    continue
                if f is None or f > t1:
                    continue
                target = p.parent if p.type == "SmartExposure" else p
                if target is None:
                    continue
                if best is None or f < best[0]:
                    best = (f, target)
        return best

    def advance(self, dur):
        t1 = min(self.t + dur, HORIZON)
        hit = self._interrupt_at(self.t, t1) if dur > 0 else None
        if hit:
            self.t = hit[0]
            raise _Interrupt(hit[1])
        self.t = t1
        if self.t >= HORIZON:
            raise _End()

    # execution
    def reset(self, n):
        n.status = "CREATED"
        for c in n.conds:
            c.completed = 0
        for i in n.items:
            self.reset(i)

    def run_item(self, n):
        self.steps += 1
        assert self.steps < 200000, "runaway loop in the simulated sequence"
        n.status = "RUNNING"
        if n.is_container:
            self.run_container(n)
        else:
            self.run_instruction(n)
        n.status = "FINISHED"

    def run_container(self, c):
        self.stack.append(c)
        try:
            c.iterations = 0
            while True:
                nxt = next((i for i in c.items if i.status == "CREATED"), None)
                if nxt is None or not self.can_continue(c, nxt):
                    break
                while nxt is not None and self.can_continue(c, nxt):
                    self.run_item(nxt)
                    nxt = next((i for i in c.items if i.status == "CREATED"), None)
                c.iterations += 1
                for cond in c.conds:
                    if cond.type == "LoopCondition":
                        cond.completed += 1
                if not self.can_continue(c, None):
                    break
                for i in c.items:
                    self.reset(i)
        except _Interrupt as e:
            if e.target is not c:
                raise
        finally:
            self.stack.pop()

    def run_instruction(self, n):
        ty = n.type
        if ty == "TakeExposure":
            start = self.t
            kind = str(n.d.get("ImageType", "LIGHT")).upper()
            try:
                self.advance(self.duration(n))
            except _Interrupt:
                if kind == "LIGHT":
                    self.lights.append({"start": start, "end": self.t, "done": False})
                raise
            if kind == "LIGHT":
                self.lights.append({"start": start, "end": self.t, "done": True})
            elif kind == "DARK":
                self.darks += 1
        elif ty == "WaitUntilSafe":
            self.advance(self._next_safe(self.t) - self.t)
        elif ty == "WaitForTime":
            target = TIMES[_short(n.d["SelectedProvider"]["$type"])] \
                + 60 * n.d.get("MinutesOffset", 0)
            if target > self.t:
                self.advance(target - self.t)
        elif ty == "WaitForTimeSpan":
            self.advance(float(n.d.get("Time", 0)))
        elif ty == "RunAutofocus":
            self.advance(60)
        elif ty == "Center":
            self.advance(30)

    def run(self):
        try:
            self.run_item(self.root)
        except _End:
            pass
        return self


UNSAFE_AT = 8.5 * 3600 + 17                         # mid-exposure, like 11:39:44Z


def test_sim_reproduces_2026_09_26_on_the_deployed_shape():
    """Old JSON: the SmartExposure keeps shooting after the roof closes AND
    after the loop end, exactly the 09-26 pattern (0024-0030)."""
    sim = NinaSim(_as_deployed(_gen()), unsafe=[(UNSAFE_AT, HORIZON)]).run()
    after = [x for x in sim.lights if x["start"] >= UNSAFE_AT]
    assert len(after) >= 5
    assert any(x["start"] >= LOOP_END for x in sim.lights)


def test_sim_new_sequence_stops_within_seconds_of_unsafe():
    sim = NinaSim(_gen(), unsafe=[(UNSAFE_AT, HORIZON)]).run()
    assert sim.lights, "the night never imaged"
    assert not [x for x in sim.lights if x["start"] >= UNSAFE_AT]
    in_flight = [x for x in sim.lights if x["start"] < UNSAFE_AT < x["end"] + 1]
    assert in_flight and not in_flight[-1]["done"]
    assert in_flight[-1]["end"] <= UNSAFE_AT + 6        # 5 s watchdog
    assert sim.darks > 0                                # UNSAFE branch ran


def test_sim_guards_hold_even_without_parent_links():
    """Belt and braces: the SmartExposure's own Safety condition stops the
    next exposure even if NINA ever dropped the links (the in-flight sub
    completes, as the Piggy-600's did on 09-26)."""
    sim = NinaSim(_strip_links(_gen()), unsafe=[(UNSAFE_AT, HORIZON)]).run()
    assert not [x for x in sim.lights if x["start"] >= UNSAFE_AT]


def test_sim_no_light_runs_past_the_loop_end_on_a_clear_night():
    sim = NinaSim(_gen(oiii=200, ha=200)).run()
    assert sim.lights
    assert max(x["end"] for x in sim.lights) <= LOOP_END
    old = NinaSim(_as_deployed(_gen(oiii=200, ha=200))).run()
    assert any(x["start"] >= LOOP_END for x in old.lights)
    stripped = NinaSim(_strip_links(_gen(oiii=200, ha=200))).run()
    assert not [x for x in stripped.lights if x["start"] >= LOOP_END]


def test_sim_dawn_flats_run_once_not_until_the_shutdown():
    """DAWN_SKY_FLATS carries a SafetyMonitorCondition, so it needs a
    LoopCondition(1) or it reshoots the flat set while safe (found by this
    simulator; the RC16 never reached its flat window before PS-36)."""
    sim = NinaSim(_gen(oiii=200, ha=200))
    flats = []
    orig = sim.run_instruction

    def spy(n):
        if n.type == "TakeExposure" and n.d.get("ImageType") == "FLAT":
            flats.append(sim.t)
        return orig(n)

    sim.run_instruction = spy
    sim.run()
    n = PhotonScriptConfig().flat_count
    assert 0 < len(flats) <= n * 7 and len(flats) % n == 0


def test_sim_pause_then_resume_after_confirm_window():
    a, b = 4 * 3600 + 30, 5 * 3600
    sim = NinaSim(_gen(oiii=200, ha=200), unsafe=[(a, b)]).run()
    assert not [x for x in sim.lights if a <= x["start"] < b]
    resumed = [x for x in sim.lights if x["start"] >= b]
    assert resumed, "night loop never resumed after the pause"
    confirm = PhotonScriptConfig().safety_confirm_seconds
    assert min(x["start"] for x in resumed) >= b + confirm


# --- 3. armer defense in depth ---------------------------------------------------

from photonscript.scheduler.armer import Armer  # noqa: E402
import photonscript.scheduler.armer as armer_mod  # noqa: E402

TREE_IMAGING = [{"GlobalTriggers": []},
                {"Name": "Targets_Container", "Status": "RUNNING", "Items": [
                    {"Name": "LOOP_ALL_NIGHT_Container", "Status": "RUNNING",
                     "Items": [{"Name": "SAFE_LOOP_Container", "Status": "RUNNING",
                                "Items": []},
                               {"Name": "UNSAFE_Container", "Status": "CREATED",
                                "Items": []}]}]}]
TREE_PARKED = [{"GlobalTriggers": []},
               {"Name": "Targets_Container", "Status": "RUNNING", "Items": [
                   {"Name": "LOOP_ALL_NIGHT_Container", "Status": "RUNNING",
                    "Items": [{"Name": "SAFE_LOOP_Container", "Status": "FINISHED",
                               "Items": []},
                              {"Name": "UNSAFE_Container", "Status": "RUNNING",
                               "Items": []}]}]}]


def _paused_armer(tmp_path, monkeypatch, tree, safe=False, **cfg):
    a = Armer(PhotonScriptConfig(data_dir=str(tmp_path), **cfg))
    a.state = "PAUSED_UNSAFE"
    a.plan = {"dawn_utc": "2026-09-27T11:48:14Z", "dusk_utc": "2026-09-27T02:26:50Z",
              "night_of": "2026-09-26"}
    calls, notes = [], []
    env = {"tree": tree, "safe": safe}

    async def fake_nina(key, method="GET", json_body=None, **kw):
        calls.append(key)
        if key == "safety":
            return {"Response": {"Connected": True, "IsSafe": env["safe"]}}
        if key == "sequence_json":
            return None if env["tree"] is None else {"Response": env["tree"]}
        return {"Response": "ok"}

    async def fake_notify(cfg_, msg, **kw):
        notes.append((kw.get("title"), msg))

    a._nina = fake_nina
    monkeypatch.setattr(armer_mod, "notify", fake_notify)
    return a, calls, notes, env


@pytest.mark.asyncio
async def test_armer_stops_nina_that_keeps_imaging_while_unsafe(tmp_path, monkeypatch):
    a, calls, notes, _ = _paused_armer(tmp_path, monkeypatch, TREE_IMAGING)
    t0 = datetime(2026, 9, 27, 11, 39, 56)
    a._unsafe_since = t0
    await a._maybe_stop_stuck_imaging(t0 + timedelta(seconds=60))
    assert "sequence_stop" not in calls                 # still in grace
    await a._maybe_stop_stuck_imaging(t0 + timedelta(seconds=121))
    assert calls[-3:] == ["sequence_stop", "guider_stop", "mount_park"]
    assert "camera_warm" not in calls                   # cooler kept on
    assert a._unsafe_stopped and a.state == "PAUSED_UNSAFE"
    assert notes and notes[-1][0] == "PhotonScript SAFETY STOP"
    n = len(calls)
    await a._maybe_stop_stuck_imaging(t0 + timedelta(seconds=180))
    assert len(calls) == n                              # one verdict per episode
    saved = json.loads((tmp_path / "armer_state.json").read_text())
    assert saved["unsafe_stopped"] is True


@pytest.mark.asyncio
async def test_armer_leaves_a_properly_parked_night_loop_alone(tmp_path, monkeypatch):
    a, calls, notes, _ = _paused_armer(tmp_path, monkeypatch, TREE_PARKED)
    t0 = datetime(2026, 9, 27, 11, 39, 56)
    a._unsafe_since = t0
    await a._maybe_stop_stuck_imaging(t0 + timedelta(seconds=200))
    assert "sequence_stop" not in calls and not a._unsafe_stopped


@pytest.mark.asyncio
async def test_armer_fails_safe_when_the_tree_is_unreadable(tmp_path, monkeypatch):
    a, calls, _, _ = _paused_armer(tmp_path, monkeypatch, None)
    t0 = datetime(2026, 9, 27, 11, 39, 56)
    a._unsafe_since = t0
    for k in range(2):
        await a._maybe_stop_stuck_imaging(t0 + timedelta(seconds=130 + 30 * k))
        assert "sequence_stop" not in calls
    await a._maybe_stop_stuck_imaging(t0 + timedelta(seconds=190))
    assert "sequence_stop" in calls and a._unsafe_stopped


@pytest.mark.asyncio
async def test_armer_unsafe_stop_can_be_disabled(tmp_path, monkeypatch):
    a, calls, _, _ = _paused_armer(tmp_path, monkeypatch, TREE_IMAGING,
                                   unsafe_stop_enabled=False)
    a._unsafe_since = datetime(2026, 9, 27, 11, 0, 0)
    await a._maybe_stop_stuck_imaging(datetime(2026, 9, 27, 11, 30, 0))
    assert "sequence_stop" not in calls


@pytest.mark.asyncio
async def test_resume_after_safety_stop_redispatches_after_confirm(tmp_path, monkeypatch):
    a, calls, notes, env = _paused_armer(tmp_path, monkeypatch, TREE_PARKED, safe=True)
    a._unsafe_stopped = True
    seen = []

    async def fake_dispatch(companion=True, fail_state="ERROR"):
        seen.append((companion, fail_state))
        return True

    a._dispatch_and_start = fake_dispatch
    t = datetime(2026, 9, 27, 8, 0, 0)
    await a._resume_after_safety_stop(t)
    await a._resume_after_safety_stop(t + timedelta(seconds=60))
    assert not seen and a.state == "PAUSED_UNSAFE"      # confirming
    await a._resume_after_safety_stop(t + timedelta(seconds=121))
    assert seen == [(False, None)]                     # no companion restart
    assert a.state == "RUNNING" and not a._unsafe_stopped


@pytest.mark.asyncio
async def test_no_redispatch_with_too_little_dark_left(tmp_path, monkeypatch):
    a, _, _, _ = _paused_armer(tmp_path, monkeypatch, TREE_PARKED, safe=True)
    a._unsafe_stopped = True

    async def boom(**kw):
        raise AssertionError("must not re-dispatch")

    a._dispatch_and_start = boom
    t = datetime(2026, 9, 27, 11, 20, 0)                 # 28 min to astro dawn
    a._safe_since = t - timedelta(seconds=600)
    await a._resume_after_safety_stop(t)
    assert a.state == "PAUSED_UNSAFE" and "dawn shutdown" in a.detail


@pytest.mark.asyncio
async def test_failed_redispatch_stays_paused_so_dawn_shutdown_still_runs(
        tmp_path, monkeypatch):
    a, _, _, _ = _paused_armer(tmp_path, monkeypatch, TREE_PARKED, safe=True)
    a._unsafe_stopped = True

    async def fail(**kw):
        return False

    a._dispatch_and_start = fail
    t = datetime(2026, 9, 27, 6, 0, 0)
    a._safe_since = t - timedelta(seconds=600)
    await a._resume_after_safety_stop(t)
    assert a.state == "PAUSED_UNSAFE" and a._unsafe_stopped


@pytest.mark.asyncio
async def test_tick_running_to_paused_then_stop(tmp_path, monkeypatch):
    """End to end through _tick: RUNNING reads unsafe -> PAUSED_UNSAFE; two
    minutes later NINA is still in SAFE_LOOP -> the armer stops it."""
    a, calls, notes, env = _paused_armer(tmp_path, monkeypatch, TREE_IMAGING)
    a.state = "RUNNING"
    clock = {"now": datetime(2026, 9, 27, 11, 39, 56)}

    class _DT(datetime):
        @classmethod
        def utcnow(cls):
            return clock["now"]

    monkeypatch.setattr(armer_mod, "datetime", _DT)
    await a._tick()
    assert a.state == "PAUSED_UNSAFE" and "sequence_stop" not in calls
    assert "armer checks" in notes[-1][1]
    clock["now"] += timedelta(seconds=150)
    await a._tick()
    assert "sequence_stop" in calls and a._unsafe_stopped


def test_shutdown_verify_waits_out_ninas_warm_window():
    a = Armer(PhotonScriptConfig())
    assert a._shutdown_verify_delay_s() >= 17 * 60
    b = Armer(PhotonScriptConfig(gradual_warm_minutes=10))
    assert b._shutdown_verify_delay_s() >= 27 * 60
