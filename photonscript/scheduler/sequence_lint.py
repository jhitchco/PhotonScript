"""Sequence linter — validates NINA Advanced Sequencer JSON before dispatch.

Encodes hard-won AARO operational rules. A sequence must pass with zero
errors before it is sent to the telescope. Catches the failure modes that
otherwise surface at 3 AM with nobody watching.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field


@dataclass
class Finding:
    level: str   # "ERROR" | "WARN"
    rule: str
    detail: str


@dataclass
class LintResult:
    findings: list[Finding] = field(default_factory=list)

    def error(self, rule: str, detail: str):
        self.findings.append(Finding("ERROR", rule, detail))

    def warn(self, rule: str, detail: str):
        self.findings.append(Finding("WARN", rule, detail))

    @property
    def ok(self) -> bool:
        return not any(f.level == "ERROR" for f in self.findings)


def _walk(node, path=""):
    """Yield (path, dict) for every dict in the tree, depth-first, in order."""
    if isinstance(node, dict):
        yield path, node
        for k, v in node.items():
            yield from _walk(v, f"{path}.{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _walk(v, f"{path}[{i}]")


def _types_in(node):
    return [(p, d) for p, d in _walk(node) if isinstance(d, dict) and "$type" in d]


def _has_type(node, fragment: str) -> bool:
    return any(fragment in d["$type"] for _, d in _types_in(node))


def _find_type(node, fragment: str) -> list[dict]:
    return [d for _, d in _types_in(node) if fragment in d["$type"]]


def _exec_items(node):
    """Yield sequence items in execution (document) order, descending only
    through Items. Trigger runners and conditions are skipped: an AF inside a
    trigger may never fire, so it cannot satisfy an ordering rule."""
    if not isinstance(node, dict):
        return
    for it in (node.get("Items") or {}).get("$values", []) or []:
        if isinstance(it, dict):
            yield it
            yield from _exec_items(it)


def _check_focus_moves(seq: dict, r: LintResult) -> None:
    """PS-65: a MoveFocuserAbsolute is only a starting point for autofocus. If
    a LIGHT exposure runs after it with no RunAutofocus in between, the frames
    are shot at a stale seed (2026-09-26: a July Ha seed with no AF after it
    put the RC16 ~300 steps out all night). Darks, bias and flats are not
    affected by focus and are ignored. A MoveFocuserRelative after an AF (a
    filter offset) is allowed."""
    pending = None   # position of the last absolute move not yet followed by AF
    for it in _exec_items(seq):
        t = it.get("$type", "")
        if "MoveFocuserAbsolute" in t:
            pending = it.get("Position")
        elif "RunAutofocus" in t:
            pending = None
        elif pending is not None and "Imaging.TakeExposure" in t \
                and str(it.get("ImageType", "LIGHT")).upper() == "LIGHT":
            r.error("focus-seed", f"MoveFocuserAbsolute to {pending} is followed "
                                  "by LIGHT exposures with no RunAutofocus in "
                                  "between (frames shot at a stale seed)")
            pending = None   # one finding per offending move


def _check_readout_mode(seq: dict, r: LintResult) -> None:
    """PS-128: darks and bias must be shot at the lights' camera readout mode
    (the AP26MC's HCG vs LCG: 0.25 vs 0.79 e-/ADU, a different bias and dark
    signal). PhotonScript's generated sequences never set it: NINA shoots
    every LIGHT / DARK / BIAS at the profile's "readout mode for sequence
    images", so within one sequence they always match. A hand-edited or
    sideloaded sequence can carry NINA's "Set readout mode" instruction
    (Camera category; Mode = index into the camera's readout modes): then
    every LIGHT and every DARK / BIAS exposure must run at the same mode,
    the profile's (no instruction before it) counting as its own mode.
    Flats are not checked."""
    current = None          # None = the profile's mode (no instruction yet)
    sets = False
    light_modes, cal_modes = set(), set()
    for it in _exec_items(seq):
        t = it.get("$type", "")
        if "SetReadoutMode" in t:
            sets = True
            current = it.get("Mode")
        elif "Imaging.TakeExposure" in t:
            kind = str(it.get("ImageType", "LIGHT")).upper()
            if kind == "LIGHT":
                light_modes.add(current)
            elif kind in ("DARK", "BIAS"):
                cal_modes.add(current)
    if not sets or not light_modes or not cal_modes:
        return
    if len(light_modes | cal_modes) > 1:
        def name(m):
            return "profile" if m is None else f"mode {m}"
        r.error("readout", "LIGHT exposures run at "
                + ", ".join(sorted(name(m) for m in light_modes))
                + " but DARK / BIAS at "
                + ", ".join(sorted(name(m) for m in cal_modes))
                + " (Set readout mode): darks and bias must match the lights' "
                "readout mode (HCG vs LCG)")


_ENTITY_LISTS = ("Items", "Conditions", "Triggers")


def _check_parent_links(seq: dict, r: LintResult) -> None:
    """PS-77: every sequence entity must carry a leading "$id" and every child
    a "Parent": {"$ref"} to its container. NINA sets Parent only from that
    reference; without it ancestor conditions are never checked between a
    SmartExposure's exposures and the safety/time watchdogs never interrupt
    (2026-09-26: 38 min of lights with the roof closed). Use
    nina_sequence_json.link_parents() on any generated tree."""
    problems: list[str] = []
    seen: set = set()

    def visit(node: dict, parent_id, where: str) -> None:
        nid = node.get("$id")
        if nid is None or next(iter(node)) != "$id":
            problems.append(f"{where}: no leading $id")
        elif nid in seen:
            problems.append(f"{where}: duplicate $id {nid}")
        else:
            seen.add(nid)
        if parent_id is not None:
            ref = (node.get("Parent") or {}).get("$ref") \
                if isinstance(node.get("Parent"), dict) else None
            if ref != parent_id:
                problems.append(f"{where}: Parent is {ref!r}, want {parent_id!r}")
        for key in _ENTITY_LISTS:
            for i, ch in enumerate((node.get(key) or {}).get("$values", []) or []):
                if isinstance(ch, dict):
                    visit(ch, nid, f"{where}/{ch.get('Name') or key}[{i}]")
        runner = node.get("TriggerRunner")
        if isinstance(runner, dict):
            visit(runner, None, f"{where}/TriggerRunner")

    visit(seq, None, seq.get("Name") or "root")
    if problems:
        more = f" (+{len(problems) - 3} more)" if len(problems) > 3 else ""
        r.error("parent-links", "sequence entities without NINA Parent links, "
                                "so ancestor Safety/Time conditions are never "
                                "checked between exposures: "
                                + "; ".join(problems[:3]) + more)


def _light_loops(node, path=""):
    """(path, container) for every container whose OWN Items include a LIGHT
    TakeExposure: the innermost repeating container of a light loop."""
    if isinstance(node, dict):
        here = f"{path}/{node.get('Name')}" if node.get("Name") else path
        items = (node.get("Items") or {}).get("$values") \
            if isinstance(node.get("Items"), dict) else None
        if items and any(isinstance(it, dict)
                         and "Imaging.TakeExposure" in it.get("$type", "")
                         and str(it.get("ImageType", "LIGHT")).upper() == "LIGHT"
                         for it in items):
            yield here, node
        for v in node.values():
            yield from _light_loops(v, here)
    elif isinstance(node, list):
        for v in node:
            yield from _light_loops(v, path)


def _check_light_loop_guards(seq: dict, r: LintResult) -> None:
    """PS-77: every light loop must be guarded at its innermost repeating
    container (the one that directly holds TakeExposure, e.g. the
    SmartExposure) by a SafetyMonitorCondition and a TimeCondition (the
    night's loop end). NINA checks a container's OWN conditions between its
    items no matter what; conditions on ancestors only count through Parent
    links. The Piggy-600 loop (guarded this way) stopped after one sub on
    2026-09-26; the RC16 SmartExposure (guarded only by ancestors) did not."""
    for path, c in _light_loops(seq):
        name = c.get("Name") or path or "?"
        conds = json.dumps(c.get("Conditions", {}))
        if "SafetyMonitorCondition" not in conds:
            r.error("light-loop-safety", f"[{path or name}] light loop has no "
                                         "SafetyMonitorCondition of its own: it "
                                         "keeps exposing when the roof closes")
        if "Conditions.TimeCondition" not in conds:
            r.error("light-loop-end", f"[{path or name}] light loop has no "
                                      "TimeCondition of its own: it keeps "
                                      "starting subs after the night loop's end")


# PS-132: NINA flat instructions that find their filter with
# Items.First(x is SwitchFilter) (GetSwitchFilterItem): without a SwitchFilter
# child their Validate throws "Sequence contains no matching element", NINA
# logs it every few seconds and a manual Start does nothing (NINA #2,
# 2026-10-05).
FLAT_FILTER_TYPES = ("FlatDevice.SkyFlat", "FlatDevice.AutoExposureFlat",
                     "FlatDevice.AutoBrightnessFlat",
                     "FlatDevice.TrainedFlatExposure",
                     "FlatDevice.TrainedDarkFlatExposure")


def _check_flat_filters(seq: dict, r: LintResult,
                        filter_wheel: bool = True) -> None:
    """PS-132 rules. flat-filter (any rig): every NINA flat instruction in
    FLAT_FILTER_TYPES needs a SwitchFilter among its own Items. On a rig
    without a filter wheel (filter_wheel=False, rigs.rig_has_filter_wheel)
    that SwitchFilter must carry no filter (Filter null validates clean with
    no wheel), and no-filter-wheel flags any SwitchFilter with a filter set:
    NINA reports "filter wheel not connected" and the step cannot run."""
    for _p, d in _types_in(seq):
        t = d["$type"]
        if not any(f in t for f in FLAT_FILTER_TYPES):
            continue
        items = (d.get("Items") or {}).get("$values", [])             if isinstance(d.get("Items"), dict) else (d.get("Items") or [])
        if not any(isinstance(it, dict) and "SwitchFilter" in it.get("$type", "")
                   for it in items):
            kind = t.split(",")[0].split(".")[-1]
            r.error("flat-filter", f"[{d.get('Name') or kind}] {kind} has no "
                    "SwitchFilter child: NINA's Validate throws (Sequence "
                    "contains no matching element) and Start does nothing. "
                    "Give it a SwitchFilter (Filter null on a rig without a "
                    "filter wheel)")
    if filter_wheel:
        return
    set_filters = [d for d in _find_type(seq, "FilterWheel.SwitchFilter")
                   if d.get("Filter") is not None]
    if set_filters:
        names = sorted({str((d.get("Filter") or {}).get("_name", "?"))
                        for d in set_filters})
        r.error("no-filter-wheel", f"{len(set_filters)} SwitchFilter(s) select "
                f"a filter ({', '.join(names)}) on a rig without a filter "
                "wheel: NINA reports the wheel not connected")


def _cooling_limits(rig: str, setpoint: float | None):
    """PS-154: (setpoint, tolerance C, bound min) from the config:
    the rig's setpoint unless one is given, cooler_gate_tolerance_c and
    cooler_gate_timeout_min. Safe defaults if the config can't be read."""
    tol, bound, sp = 1.0, 20, setpoint
    try:
        from photonscript.shared.config import PhotonScriptConfig
        from photonscript.shared.rigs import rig_setpoint
        from photonscript.scheduler.nina_sequence_json import cool_bound_minutes
        c = PhotonScriptConfig()
        tol = float(getattr(c, "cooler_gate_tolerance_c", 1.0))
        bound = cool_bound_minutes(c)
        if sp is None:
            sp = rig_setpoint(c, rig)
    except Exception:
        pass
    return (0.0 if sp is None else float(sp)), tol, bound


def _span_minutes(cond: dict) -> float | None:
    """A TimeSpanCondition's span in minutes, else None."""
    if "TimeSpanCondition" not in str(cond.get("$type", "")):
        return None
    try:
        return (float(cond.get("Hours", 0) or 0) * 60
                + float(cond.get("Minutes", 0) or 0)
                + float(cond.get("Seconds", 0) or 0) / 60)
    except (TypeError, ValueError):
        return None


def _cools_with_bounds(seq: dict) -> list[tuple[dict, float | None]]:
    """Every CoolCamera with the tightest TimeSpanCondition span (min) on
    its own or an ancestor container (None = nothing bounds it). NINA's
    CoolCamera waits until the sensor is within 1 C of Temperature with no
    timeout of its own; only an interrupting condition (TimeSpanCondition's
    1 s watchdog) ends that wait."""
    out: list[tuple[dict, float | None]] = []

    def visit(node: dict, bound: float | None) -> None:
        spans = [m for m in (_span_minutes(c) for c in
                             ((node.get("Conditions") or {}).get("$values", [])
                              if isinstance(node.get("Conditions"), dict) else [])
                             if isinstance(c, dict)) if m is not None]
        if spans:
            bound = min(spans + ([bound] if bound is not None else []))
        items = ((node.get("Items") or {}).get("$values", [])
                 if isinstance(node.get("Items"), dict) else [])
        for it in items:
            if not isinstance(it, dict):
                continue
            if "CoolCamera" in str(it.get("$type", "")):
                out.append((it, bound))
            if isinstance(it.get("Items"), dict):
                visit(it, bound)

    if isinstance(seq, dict):
        visit(seq, None)
    return out


def check_cooling(seq: dict, r: LintResult, setpoint: float | None = None,
                  rig: str = "rc16", strict: bool = True) -> None:
    """Rule cooling (PS-154, 2026-10-06: a CoolCamera at -10 C with the rig
    configured for 0 C waited all night and the mount never unparked).
    ERROR: a CoolCamera warmer than 0 C (never a warm sensor), one whose
    Temperature is more than cooler_gate_tolerance_c off the rig's setpoint,
    and (strict) one no TimeSpanCondition bounds to cooler_gate_timeout_min
    plus its ramp. strict False (a hand-built sideload) makes the bound rule
    a WARN: the operator built it on purpose, the stall alarm still pages."""
    sp, tol, bound = _cooling_limits(rig, setpoint)
    pairs = _cools_with_bounds(seq)
    if not pairs:
        r.warn("cooling", "No CoolCamera instruction found")
    for c, span in pairs:
        temp = c.get("Temperature")
        try:
            t = float(temp)
        except (TypeError, ValueError):
            t = None
        if t is None or t > 0.0:
            r.error("cooling", f"CoolCamera Temperature is {temp!r}: must be at "
                               "or below 0.0 C (never a warm sensor)")
        elif abs(t - sp) > tol:
            r.error("cooling", f"CoolCamera Temperature is {t:g} C but the {rig} "
                    f"setpoint is {sp:g} C (tolerance {tol:g} C): the sequence "
                    "would wait for a temperature PhotonScript never holds")
        try:
            ramp = max(0.0, float(c.get("Duration", 0) or 0))
        except (TypeError, ValueError):
            ramp = 0.0
        limit = bound + math.ceil(ramp)
        if span is None or span > limit + 1e-6:
            msg = ("CoolCamera with no TimeSpanCondition bound" if span is None
                   else f"CoolCamera bounded at {span:g} min (limit {limit:g})")
            msg += (": NINA waits for the setpoint with no timeout, so a cooler "
                    "that cannot reach it holds the whole night (wrap it as "
                    "nina_sequence_json._cool_camera_bounded does)")
            if strict:
                r.error("cooling", msg)
            else:
                r.warn("cooling", msg)


def _cooler_gate_wanted() -> tuple[str, str | None]:
    """PS-61: (mode, script path when the gate should be in the sequence,
    else None). ("off", None) if the config can't be read."""
    try:
        from photonscript.shared.config import PhotonScriptConfig
        from photonscript.scheduler.cooler_gate import gate_mode, gate_script
        c = PhotonScriptConfig()
        return gate_mode(c), gate_script(c)
    except Exception:
        return "off", None


def _is_cooler_gate(item: dict) -> bool:
    from photonscript.scheduler.cooler_gate import GATE_SCRIPT_TOKEN
    return ("ExternalScript" in item.get("$type", "")
            and GATE_SCRIPT_TOKEN in str(item.get("Script", "")).lower())


def _check_cooler_gate(seq: dict, r: LintResult,
                       expected: bool | None = None) -> None:
    """PS-61 rule cooler-gate: with the gate on, every LIGHT TakeExposure
    must be preceded, in its own or an ancestor container, by the
    cooler-gate ExternalScript, so no light starts before the sensor is at
    its setpoint. In skip mode the gate must carry ErrorBehavior 1
    (SkipInstructionSetOnError) or a timeout would not skip anything.
    expected=None reads the config (mode on and the script present on this
    machine); mode on with the script missing is a warning (the generator
    then emits no gate and says so in the sequence)."""
    mode, script = _cooler_gate_wanted()
    if expected is None:
        if mode != "off" and script is None:
            r.warn("cooler-gate", "cooler gate is on but its script was not "
                                  "found on this machine: lights are not held "
                                  "for the setpoint tonight")
        expected = script is not None
    if not expected:
        return
    ungated: list[str] = []
    gates: list[dict] = []

    def visit(node: dict, gated: bool, path: str) -> None:
        items = (node.get("Items") or {}).get("$values", []) \
            if isinstance(node.get("Items"), dict) else []
        here = f"{path}/{node.get('Name')}" if node.get("Name") else path
        for it in items:
            if not isinstance(it, dict):
                continue
            if _is_cooler_gate(it):
                gates.append(it)
                gated = True
                continue
            if ("Imaging.TakeExposure" in it.get("$type", "")
                    and str(it.get("ImageType", "LIGHT")).upper() == "LIGHT"
                    and not gated):
                ungated.append(here or "?")
            if isinstance(it.get("Items"), dict):
                visit(it, gated, here)

    visit(seq, False, "")
    if ungated:
        uniq = sorted(set(ungated))
        more = f" (+{len(uniq) - 3} more)" if len(uniq) > 3 else ""
        r.error("cooler-gate", f"{len(uniq)} light loop(s) start without the "
                "cooler gate before them, so they can shoot off the setpoint: "
                + "; ".join(uniq[:3]) + more)
    if mode == "skip":
        bad = [g for g in gates if g.get("ErrorBehavior") != 1]
        if bad:
            r.error("cooler-gate", f"{len(bad)} cooler gate(s) without "
                    "ErrorBehavior 1 (SkipInstructionSetOnError): a timeout "
                    "would not skip the block")
    if any(int(g.get("Attempts", 1) or 1) > 1 for g in gates):
        r.warn("cooler-gate", "a cooler gate has Attempts > 1: each retry "
                              "holds another full timeout")


def _settle_gate_wanted() -> tuple[bool, str | None]:
    """PS-27: (gate on, script path when it should be in the OSC light loop
    else None). (False, None) if the config can't be read."""
    try:
        from photonscript.shared.config import PhotonScriptConfig
        from photonscript.scheduler.split_guard import gate_enabled, gate_script
        c = PhotonScriptConfig()
        return gate_enabled(c), gate_script(c)
    except Exception:
        return False, None


def _check_settle_gate(seq: dict, r: LintResult,
                       expected: bool | None = None) -> None:
    """PS-27 rule settle-gate: in the Piggy-600 companion, OSC_LIGHT_LOOP
    must come right after the settle-gate ExternalScript in its parent, and
    every LIGHT TakeExposure in it must be followed right away by the gate
    (the gate sits after the light so the loop's dawn TimeCondition sees the
    exposure next), so each OSC sub starts on a still mount. The gate must
    never skip (ErrorBehavior 0). expected=None reads the config (on and the
    script present on this machine); on with the script missing is a
    warning. Sequences without an OSC light loop (the RC16) are not
    checked."""
    from photonscript.scheduler.calibration import OSC_LIGHT_LOOP_NAME
    from photonscript.scheduler.split_guard import GATE_SCRIPT_TOKEN
    def kids(node):
        return [i for i in (node.get("Items") or {}).get("$values", [])
                if isinstance(i, dict)] if isinstance(node.get("Items"), dict) else []

    parents = [(d, k) for d in _walk_dicts(seq) for k, i in enumerate(kids(d))
               if i.get("Name") == OSC_LIGHT_LOOP_NAME]
    loops = [kids(d)[k] for d, k in parents]
    if not loops:
        return
    on, script = _settle_gate_wanted()
    if expected is None:
        if on and script is None:
            r.warn("settle-gate", "settle gate is on but its script was not "
                                  "found on this machine: OSC lights do not "
                                  "wait for a still mount tonight")
        expected = script is not None
    if not expected:
        return

    def is_gate(it):
        return (isinstance(it, dict) and "ExternalScript" in it.get("$type", "")
                and GATE_SCRIPT_TOKEN in str(it.get("Script", "")).lower())

    ungated = skipping = 0
    for parent, k in parents:
        if not (k > 0 and is_gate(kids(parent)[k - 1])):
            ungated += 1                       # first light of the loop
    gates = [i for d in _walk_dicts(seq) for i in kids(d) if is_gate(i)]
    skipping = sum(1 for g in gates if g.get("ErrorBehavior", 0) != 0)
    for loop in loops:
        items = kids(loop)
        for k, it in enumerate(items):
            if ("Imaging.TakeExposure" in it.get("$type", "")
                    and str(it.get("ImageType", "LIGHT")).upper() == "LIGHT"
                    and not (k + 1 < len(items) and is_gate(items[k + 1]))):
                ungated += 1
    if ungated:
        r.error("settle-gate", f"{ungated} place(s) in {OSC_LIGHT_LOOP_NAME} "
                "where an OSC light can start without passing the settle "
                "gate (missing before the loop or after a light): it can "
                "start while the RC16 mount moves")
    if skipping:
        r.error("settle-gate", f"{skipping} settle gate(s) with ErrorBehavior "
                "other than 0: the gate must never skip a Piggy-600 light")


# PS-149: instructions that take time whenever they run. Inside a loop such
# an item either runs (the pass costs time) or a TimeCondition fails for its
# estimated duration and ends the loop, so it can never be passed over at no
# cost. WaitForTime is not one: once its time has passed it returns at once.
PACING_TYPES = ("Utility.WaitForTimeSpan", "Imaging.TakeExposure",
                "Autofocus.RunAutofocus")


def _cls(d: dict) -> str:
    return str(d.get("$type") or "").split(",")[0].strip()


def _is_loop_condition(c: dict) -> bool:
    return _cls(c).endswith(".LoopCondition")


def _repeats(node: dict) -> bool:
    """NINA repeats a container while its conditions hold. A LoopCondition
    bounds it (it runs at most Iterations passes); any other condition
    alone (Time, Safety, LoopWhileUnsafe, Altitude ...) can hold for
    ever."""
    conds = _vals(node.get("Conditions"))
    return bool(conds) and not any(_is_loop_condition(c) for c in conds)


def _may_be_noop(node: dict) -> bool:
    """A child container is a no-op (finished in no time) when its own
    conditions fail as it starts: any condition other than a LoopCondition
    with Iterations >= 1 can."""
    for c in _vals(node.get("Conditions")):
        if not _is_loop_condition(c):
            return True
        try:
            if int(c.get("Iterations", 0)) < 1:
                return True
        except (TypeError, ValueError):
            return True
    return False


def _paces(item: dict) -> bool:
    """Does running this item surely take time (or end the loop)?"""
    if isinstance(item.get("Items"), dict):
        if _may_be_noop(item):
            return False
        return any(_paces(ch) for ch in _vals(item.get("Items")))
    cls = _cls(item)
    if not any(cls.endswith("." + p) for p in PACING_TYPES):
        return False
    if cls.endswith(".WaitForTimeSpan"):
        try:
            return float(item.get("Time", 0)) >= 1
        except (TypeError, ValueError):
            return False
    return True


def _check_loop_spin(seq: dict, r: LintResult) -> None:
    """PS-149 rule loop-spin: a container that repeats (_repeats) must hold,
    outside any child container that can be a no-op, an item that surely
    takes time (_paces). NINA re-checks a loop's conditions after a pass
    with no next item (estimated 0 s), so a loop whose children can all be
    skipped re-runs at once, thousands of passes a second: NINA #2 on
    2026-10-06 06:20, OSC_LIGHTS_UNTIL_DAWN in the 30 s before nautical
    dawn (every child a conditioned wait or pass)."""
    bad: list[str] = []
    for d in _walk_dicts(seq):
        if not (isinstance(d.get("Items"), dict) and "$type" in d):
            continue
        if not _repeats(d) or any(_paces(ch) for ch in _vals(d.get("Items"))):
            continue
        kinds = ", ".join(_short_type(c.get("$type", ""))
                          for c in _vals(d.get("Conditions")))
        bad.append(f"{d.get('Name') or _short_type(d['$type'])} ({kinds})")
    if bad:
        more = f" (+{len(bad) - 3} more)" if len(bad) > 3 else ""
        r.error("loop-spin", f"{len(bad)} loop(s) with no waiting item can "
                "spin: when every child is skipped NINA repeats the loop at "
                "once (a busy loop, thousands of passes a second). Add a "
                "WaitForTimeSpan (30-60 s) to the loop itself: "
                + "; ".join(bad[:3]) + more)


def _walk_dicts(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk_dicts(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk_dicts(v)


def _force_cal_wanted() -> bool:
    """PS-72: does the config ask the night's first StartGuiding to force a
    PHD2 calibration? Defaults to False if the config can't be read."""
    try:
        from photonscript.shared.config import PhotonScriptConfig
        return bool(getattr(PhotonScriptConfig(), "guiding_force_first_calibration", False))
    except Exception:
        return False


def _selftest_wanted() -> tuple[bool, str]:
    """PS-92: (slots enabled, script path) from the config; (False, "") if
    the config can't be read."""
    try:
        from photonscript.shared.config import PhotonScriptConfig
        c = PhotonScriptConfig()
        return (bool(getattr(c, "phd2_selftest_enabled", False)),
                str(getattr(c, "phd2_selftest_script", "") or ""))
    except Exception:
        return False, ""


CAL_CONTAINER = "PHD2_CALIBRATION"   # phd2_calibration.CONTAINER_NAME (PS-93)


def _cal_containers(seq: dict) -> list[dict]:
    return [d for _, d in _types_in(seq) if d.get("Name") == CAL_CONTAINER
            and "Container" in d.get("$type", "")]


def _start_guiding_outside_cal(seq: dict) -> list[dict]:
    """StartGuiding items in execution order, the PS-93 calibration slot's
    own left out."""
    inside = {id(d) for c in _cal_containers(seq) for d in _find_type(c, "StartGuiding")}
    return [d for d in _exec_items(seq) if "StartGuiding" in d.get("$type", "")
            and id(d) not in inside]


def _check_calibration_slot(seq: dict, r: LintResult) -> None:
    """PS-93 rule phd2-calibration: ForceCalibration only inside the
    PHD2_CALIBRATION slot when the sequence has one; the slot forces the
    calibration and stops guiding again before the hold (the next target's
    slew must never run under a guiding PHD2)."""
    cals = _cal_containers(seq)
    if not cals:
        return
    forced = [d for d in _start_guiding_outside_cal(seq) if d.get("ForceCalibration")]
    if forced:
        r.error("phd2-calibration", f"{len(forced)} StartGuiding outside "
                f"{CAL_CONTAINER} sets ForceCalibration (the calibration slot "
                "replaces it)")
    for c in cals:
        items = [d.get("$type", "") for d in _exec_items(c)]
        sg = next((i for i, t in enumerate(items) if "StartGuiding" in t), None)
        stop = next((i for i, t in enumerate(items) if "StopGuiding" in t), None)
        start = _find_type(c, "StartGuiding")
        if sg is None or not start or not start[0].get("ForceCalibration"):
            r.error("phd2-calibration", f"{CAL_CONTAINER} has no StartGuiding "
                    "with ForceCalibration")
        elif stop is None or stop < sg:
            r.error("phd2-calibration", f"{CAL_CONTAINER} does not stop guiding "
                    "after the calibration")
        if _has_type(c, "MeridianFlipTrigger"):
            r.error("phd2-calibration", f"{CAL_CONTAINER} must not carry a "
                    "meridian flip trigger")


def _check_selftest(seq: dict, r: LintResult) -> None:
    """PS-92 rule phd2-selftest: with the self-test enabled, every
    StartGuiding must be directly preceded by the self-test ExternalScript
    (the mount's pulse path is tested before PHD2 is asked to guide), and the
    script file should exist on this machine (a missing script fails in NINA
    harmlessly, ErrorBehavior 0, so that is a warning)."""
    enabled, path = _selftest_wanted()
    if not enabled:
        return
    name = path.replace("\\", "/").rsplit("/", 1)[-1].lower()
    missing = 0

    def visit(node):
        nonlocal missing
        if node.get("Name") == CAL_CONTAINER:
            return   # PS-93: the calibration slot runs after the twilight test
        items = (node.get("Items") or {}).get("$values", []) if isinstance(
            node.get("Items"), dict) else []
        prev = None
        for it in items:
            if not isinstance(it, dict):
                continue
            if "StartGuiding" in it.get("$type", ""):
                ok = (prev is not None and "ExternalScript" in prev.get("$type", "")
                      and name and name in str(prev.get("Script", "")).lower())
                if not ok:
                    missing += 1
            prev = it
            visit(it)
    visit(seq)
    if missing:
        r.error("phd2-selftest", f"{missing} StartGuiding item(s) not preceded by "
                                 f"the pulse self-test script ({path}); "
                                 "PS_PHD2_SELFTEST_ENABLED is on")
    if path:
        from pathlib import Path
        if not Path(path).exists():
            r.warn("phd2-selftest", f"self-test script {path} not found on this "
                                    "machine (NINA would skip it)")


# PS-139: instructions that move (or re-point) the shared mount. The
# Piggy-600 rides the RC16 mount, which NINA #1 owns (PS-25). On 2026-10-03 a
# hand-built NINA #2 sequence from NINA's stock deep-sky template ran a
# Center inside its per-sub loop: every sub a slew, a solve, a refused sync
# and a ~58' offset slew. On a dual-rig night that pulls the RC16 off target
# for every Piggy sub. Matched on the exact class (namespace.Class) of
# "$type"; a ninaAPI /sequence/state or /sequence/json tree carries no
# "$type", so its items match on NINA's display name (MOUNT_DISPLAY_NAMES).
MOUNT_CLASSES = {
    "Telescope.SlewScopeToRaDec": "Slew to Ra/Dec",
    "Telescope.SlewScopeToAltAz": "Slew to Alt/Az",
    "Platesolving.Center": "Center (slew and center)",
    "Platesolving.CenterAndRotate": "Center and rotate",
    "Platesolving.SolveAndSync": "Solve and sync",
    "Telescope.SetTracking": "Set tracking",
    "Telescope.ParkScope": "Park scope",
    "Telescope.UnparkScope": "Unpark scope",
    "Telescope.FindHome": "Find home",
    "MeridianFlip.MeridianFlipTrigger": "Meridian flip trigger",
    "Platesolving.CenterAfterDriftTrigger": "Center after drift trigger",
}
MOUNT_CONNECT_LABEL = "Connect mount"
# NINA's display names (instruction Name), lower case with only letters and
# digits kept, for trees without "$type". Best effort: confirm against a
# NINA #2 /sequence/state dump.
MOUNT_DISPLAY_NAMES = {
    "slewtoradec": "Slew to Ra/Dec",
    "slewtoaltaz": "Slew to Alt/Az",
    "slewandcenter": "Center (slew and center)",
    "center": "Center (slew and center)",
    "slewcenterandrotate": "Center and rotate",
    "centerandrotate": "Center and rotate",
    "solveandsync": "Solve and sync",
    "settracking": "Set tracking",
    "parkscope": "Park scope",
    "unparkscope": "Unpark scope",
    "findhome": "Find home",
    "meridianflip": "Meridian flip trigger",
    "meridianfliptrigger": "Meridian flip trigger",
    "centerafterdrift": "Center after drift trigger",
}
_MOUNT_DEVICES = ("mount", "telescope")


def _vals(v) -> list:
    """A NINA list as a Python list: {"$values": [...]} (sequence file) or a
    bare list (ninaAPI tree)."""
    if isinstance(v, dict):
        v = v.get("$values")
    return [x for x in v if isinstance(x, dict)] if isinstance(v, list) else []


def _key(name) -> str:
    return "".join(c for c in str(name or "").lower() if c.isalnum())


def mount_label(d: dict) -> str | None:
    """The MOUNT_CLASSES label when d is a mount-moving instruction or
    trigger, else None. By "$type" when present, else by display name."""
    t = str(d.get("$type") or "")
    if t:
        cls = t.split(",")[0].strip()
        for frag, label in MOUNT_CLASSES.items():
            if cls.endswith("." + frag):
                return label
        if cls.endswith("Connect.ConnectEquipment"):
            dev = str(d.get("SelectedDevice") or "").lower()
            return MOUNT_CONNECT_LABEL if any(m in dev for m in _MOUNT_DEVICES) else None
        return None
    name = str(d.get("Name") or "")
    if not name or name.endswith("_Container") or "Items" in d:
        return None
    k = _key(name)
    if k.startswith("connect") and any(
            m in str(d.get("SelectedDevice") or "").lower() for m in _MOUNT_DEVICES):
        return MOUNT_CONNECT_LABEL
    return MOUNT_DISPLAY_NAMES.get(k)


def _runs_once(cond: dict) -> bool:
    """A LoopCondition with Iterations <= 1 (or a ninaAPI condition that
    only says Iterations <= 1): the container runs once."""
    t = str(cond.get("$type") or "")
    if t and "LoopCondition" not in t:
        return False
    try:
        return int(cond.get("Iterations")) <= 1
    except (TypeError, ValueError):
        return False


def _loops(node: dict) -> bool:
    """NINA repeats a container while its conditions hold: any condition
    other than a single-iteration LoopCondition makes it a loop."""
    return any(not _runs_once(c) for c in _vals(node.get("Conditions")))


def mount_items(tree) -> list[dict]:
    """Every mount-moving instruction or trigger in a sequence tree (a
    sequence file or a ninaAPI /sequence/state | /sequence/json payload),
    document order: {"label", "name", "where", "in_loop", "trigger",
    "status"}. in_loop: an ancestor container repeats (_loops), or the item
    is a trigger (triggers fire between exposures)."""
    out: list[dict] = []

    def visit(node: dict, path: str, in_loop: bool, trigger: bool) -> None:
        label = mount_label(node)
        name = str(node.get("Name") or "")
        if label:
            out.append({"label": label, "name": name or label,
                        "where": path or "root", "in_loop": in_loop or trigger,
                        "trigger": trigger,
                        "status": str(node.get("Status") or "") or None})
        if name.endswith("_Container"):
            name = name[: -len("_Container")]
        here = (f"{path}/{name}" if path else name) if name else path
        loops = in_loop or _loops(node)
        for ch in _vals(node.get("Items")):
            visit(ch, here, loops, trigger)
        for ch in _vals(node.get("Triggers")):
            visit(ch, here, loops, True)
        runner = node.get("TriggerRunner")
        if isinstance(runner, dict):
            visit(runner, here, loops, True)

    if isinstance(tree, dict) and "Response" in tree and "$type" not in tree:
        tree = tree.get("Response")
    for root in (tree if isinstance(tree, list) else [tree]):
        if isinstance(root, dict):
            visit(root, "", False, False)
    return out


def mount_summary(items: list[dict], limit: int = 4) -> str:
    """'Center in <where> (in a loop); ...' for alerts and lint details."""
    parts = [f"{i['label']} in {i['where']}"
             + (" (trigger)" if i.get("trigger") else
                " (in a loop)" if i.get("in_loop") else "")
             for i in items[:limit]]
    more = f" (+{len(items) - limit} more)" if len(items) > limit else ""
    return "; ".join(parts) + more


def _check_piggy_mount(seq: dict, r: LintResult, strict: bool = False) -> None:
    """PS-139 rule piggy-mount, for a Piggy-600 (NINA #2) sequence: a mount
    instruction inside a loop or a trigger is an ERROR (it moves the RC16
    on every pass); one outside any loop is a WARN (it runs once: only
    safe when the RC16 is not imaging). strict (PhotonScript's own
    companion, which never carries one): ERROR everywhere."""
    items = mount_items(seq)
    looped = [i for i in items if i["in_loop"]]
    once = [i for i in items if not i["in_loop"]]
    if looped:
        r.error("piggy-mount", f"{len(looped)} mount instruction(s) inside a "
                "loop of the Piggy-600 sequence: each pass moves the mount the "
                "RC16 rides (NINA #1 owns it): " + mount_summary(looped))
    if once:
        msg = (f"{len(once)} mount instruction(s) outside any loop of the "
               "Piggy-600 sequence (they run once): only safe while the RC16 "
               "is not imaging. Center once before the loop at most: "
               + mount_summary(once))
        if strict:
            r.error("piggy-mount", msg)
        else:
            r.warn("piggy-mount", msg)


PIGGY_CENTER_TAG = "Piggy-600 centering (PS-26)"


def _is_piggy_center(d: dict) -> bool:
    from photonscript.scheduler.nina_sequence_json import PIGGY_WEST_CENTER_SUFFIX
    return str(d.get("Name") or "").endswith(PIGGY_WEST_CENTER_SUFFIX)


def _is_optics_step(d: dict) -> bool:
    """PS-148: a through-focus step is a nested DeepSkyObjectContainer only so
    its subs carry the step's OBJECT; _check_optics_test checks it."""
    from photonscript.scheduler.nina_sequence_json import OPTICS_STEP_SUFFIX
    return str(d.get("Name") or "").endswith(OPTICS_STEP_SUFFIX)


_AF_TRIGGERS = ("AutofocusAfterHFRIncreaseTrigger",
                "AutofocusAfterTemperatureChangeTrigger",
                "AutofocusAfterExposures", "AutofocusAfterTimeTrigger",
                "AutofocusAfterFilterChange")


def _check_optics_test(tgt: dict, name: str, followed: bool,
                       r: LintResult) -> None:
    """PS-148 rule optics-test, for a target holding a through-focus sweep:
    no guiding (StartGuiding or an active dither), no autofocus trigger (it
    would refocus on a defocused step), the relative focuser moves in the
    sweep add up to zero (it ends at best focus), every step guarded by
    Safety and free of slews / centering. A WARN says what it is."""
    from photonscript.scheduler.nina_sequence_json import (
        TARGET_OPTICS_SWEEP_SUFFIX)
    guiding = (_find_type(tgt, "StartGuiding")
               + [d for d in _find_type(tgt, "DitherAfterExposures")
                  if d.get("AfterExposures", 0) > 0])
    if guiding:
        r.error("optics-test", f"[{name}] through-focus optics test contains "
                "StartGuiding or an active dither")
    af_trig = sorted({_short_type(d["$type"]) for frag in _AF_TRIGGERS
                      for d in _find_type(tgt, frag)})
    if af_trig:
        r.error("optics-test", f"[{name}] autofocus trigger(s) "
                f"{', '.join(af_trig)} would refocus on a defocused step")
    for _p, sw in _types_in(tgt):
        if not str(sw.get("Name") or "").endswith(TARGET_OPTICS_SWEEP_SUFFIX):
            continue
        net = sum(int(d.get("RelativePosition") or 0)
                  for d in _find_type(sw, "MoveFocuserRelative"))
        if net:
            r.error("optics-test", f"[{name}] the sweep's focuser moves add "
                    f"up to {net:+d} steps: it would end off focus")
    for _p, st in _types_in(tgt):
        if "DeepSkyObjectContainer" not in st.get("$type", "") \
                or not _is_optics_step(st):
            continue
        sname = (st.get("Target") or {}).get("TargetName") or st.get("Name")
        if "SafetyMonitorCondition" not in json.dumps(st.get("Conditions", {})):
            r.error("optics-test", f"[{sname}] step has no "
                    "SafetyMonitorCondition")
        if (_has_type(st, "Platesolving.Center") or _has_type(st, "SlewScope")
                or _has_type(st, "RunAutofocus")):
            r.error("optics-test", f"[{sname}] step must only expose (no "
                    "slew, center or autofocus)")
    after = ("Tonight's targets follow it; it runs once per night."
             if followed else
             "After the sweep the scope parks and holds until dawn; stop "
             "the sequence to image.")
    r.warn("optics-test", f"[{name}] through-focus optics test (PS-148): "
           "guiding stopped, short subs at best focus and at each focuser "
           "offset, back to best focus at the end. " + after)


def _is_tpoint_point(d: dict) -> bool:
    """PS-171: a TPoint mapping point is a nested DeepSkyObjectContainer
    only so its frame carries the point's OBJECT; _check_tpoint_mapping
    checks it."""
    from photonscript.scheduler.nina_sequence_json import TPOINT_POINT_PREFIX
    return str(d.get("Name") or "").startswith(TPOINT_POINT_PREFIX)


def _is_tpoint_mapping(d: dict) -> bool:
    from photonscript.scheduler.nina_sequence_json import TPOINT_MAPPING_PREFIX
    name = (d.get("Target") or {}).get("TargetName") or d.get("Name") or ""
    return str(name).startswith(TPOINT_MAPPING_PREFIX)


# Anything that would sync the mount or re-center it: a TPoint sample is the
# difference between where the mount thinks it points and where it points,
# so a sync (NINA's Center syncs; a meridian flip re-centers) erases it.
_TPOINT_FORBIDDEN = ("Platesolving.Center", "SlewScopeAndCenter",
                     "MeridianFlipTrigger", "SlewScopeToRaDec", "StartGuiding",
                     "SyncScope", "Platesolving.SolveAndSync")


def _check_tpoint_mapping(tgt: dict, name: str, followed: bool,
                          r: LintResult) -> None:
    """PS-171 rule tpoint-mapping, for a TPoint mapping target: no center,
    sync, RA/Dec slew, meridian flip trigger, guiding or active dither
    anywhere in it; every point guarded by Safety, run once, with exactly
    one Slew to Alt/Az and one LIGHT exposure. A WARN says what it is."""
    bad = sorted({_short_type(d["$type"]) for frag in _TPOINT_FORBIDDEN
                  for d in _find_type(tgt, frag)})
    bad += ["DitherAfterExposures (active)"
            for d in _find_type(tgt, "DitherAfterExposures")
            if d.get("AfterExposures", 0) > 0][:1]
    if bad:
        r.error("tpoint-mapping", f"[{name}] TPoint mapping must not center, "
                f"sync, guide or flip: found {', '.join(bad)}")
    points = [st for _p, st in _types_in(tgt)
              if "DeepSkyObjectContainer" in st.get("$type", "")
              and _is_tpoint_point(st)]
    if not points:
        r.error("tpoint-mapping", f"[{name}] has no points")
    for st in points:
        sname = st.get("Name")
        conds = json.dumps(st.get("Conditions", {}))
        if "SafetyMonitorCondition" not in conds or not any(
                _runs_once(c) for c in _vals(st.get("Conditions"))):
            r.error("tpoint-mapping", f"[{sname}] point needs its own "
                    "SafetyMonitorCondition and LoopCondition(1)")
        if (len(_find_type(st, "SlewScopeToAltAz")) != 1
                or len(_find_type(st, "Imaging.TakeExposure")) != 1
                or _has_type(st, "RunAutofocus")):
            r.error("tpoint-mapping", f"[{sname}] point must be one Slew to "
                    "Alt/Az and one exposure (then the sample script)")
    after = ("Tonight's targets follow it; it runs once per night."
             if followed else "Nothing follows it tonight.")
    r.warn("tpoint-mapping", f"[{name}] TPoint mapping (PS-171): {len(points)} "
           "blind alt/az points, no center, no sync, no flip trigger, "
           "guiding stopped. " + after)


def _short_type(t: str) -> str:
    return (t or "").split(",")[0].split(".")[-1]


def _check_piggy_center(seq: dict, r: LintResult) -> None:
    """PS-26 rule piggy-center: every Piggy-600-driven target says what its
    centering does (a WARN carrying the annotation: the applied offset, the
    preview, or "no offset measured yet"), and a nested pier-West centering
    container must run once (LoopCondition), only before the transit
    (TimeCondition), and hold nothing but its Center (no exposures)."""
    for _p, d in _types_in(seq):
        if "DeepSkyObjectContainer" not in d["$type"] or _is_piggy_center(d):
            continue
        name = (d.get("Target") or {}).get("TargetName") or d.get("Name", "?")
        for it in (d.get("Items") or {}).get("$values", []) or []:
            if isinstance(it, dict) and "Annotation" in it.get("$type", "")                     and PIGGY_CENTER_TAG in str(it.get("Text", "")):
                r.warn("piggy-center", f"[{name}] {it.get('Text')}")
    for _p, d in _types_in(seq):
        if "DeepSkyObjectContainer" not in d["$type"] or not _is_piggy_center(d):
            continue
        name = d.get("Name", "?")
        conds = json.dumps(d.get("Conditions", {}))
        if "LoopCondition" not in conds or "TimeCondition" not in conds:
            r.error("piggy-center", f"[{name}] needs LoopCondition(1) and the "
                    "transit TimeCondition (it would re-center every pass or "
                    "after the flip)")
        if not _has_type(d, "Platesolving.Center"):
            r.error("piggy-center", f"[{name}] has no Center")
        if _has_type(d, "TakeExposure") or _has_type(d, "SmartExposure"):
            r.error("piggy-center", f"[{name}] must not expose")


def lint(seq: dict, guided: bool | None = None,
         unguided_dither: bool = False,
         cooler_gate: bool | None = None,
         filter_wheel: bool = True,
         settle_gate: bool | None = None,
         setpoint: float | None = None,
         hand_built: bool = False) -> LintResult:
    """Validate a parsed sequence. guided=None auto-detects from content.
    unguided_dither (PS-66): an unguided run may carry active dithers (NINA
    Direct Guider); StartGuiding is still an error. cooler_gate (PS-61):
    require the gate before every light loop (None = from the config).
    filter_wheel (PS-132): False for a rig without a wheel.
    settle_gate (PS-27): require the settle gate before every OSC light in
    the Piggy-600 light loop (None = from the config).
    setpoint (PS-154): the RC16 setpoint every CoolCamera must match (None =
    camera_setpoint_c from the config). hand_built: a sequence someone built
    in NINA and sideloads; an unbounded CoolCamera is then a WARN."""
    r = LintResult()

    if guided is None:
        guided = _has_type(seq, "StartGuiding")

    # --- Global checks -----------------------------------------------------
    check_cooling(seq, r, setpoint=setpoint, rig="rc16",   # PS-154
                  strict=not hand_built)

    _check_focus_moves(seq, r)
    _check_parent_links(seq, r)
    _check_light_loop_guards(seq, r)
    _check_loop_spin(seq, r)   # PS-149
    _check_cooler_gate(seq, r, cooler_gate)
    _check_settle_gate(seq, r, settle_gate)
    _check_readout_mode(seq, r)
    _check_flat_filters(seq, r, filter_wheel)   # PS-132

    # PS-171: a TPoint mapping run alone (every target a mapping run) has no
    # flip trigger by design (each alt/az point sits on a fixed pier side)
    mapping_only = bool(_find_type(seq, "DeepSkyObjectContainer")) and all(
        _is_tpoint_mapping(d) or _is_tpoint_point(d)
        for d in _find_type(seq, "DeepSkyObjectContainer"))
    if not _has_type(seq, "MeridianFlipTrigger") and not mapping_only:
        r.error("meridian", "No MeridianFlipTrigger found anywhere in sequence")

    # Night-loop safety architecture (Jerry Macon pattern)
    if not _has_type(seq, "WaitUntilSafe"):
        r.error("night-loop", "No WaitUntilSafe — unsafe weather would end the "
                              "night instead of pausing it")
    dawn_conditions = [d for d in _find_type(seq, "TimeCondition")
                       if "Dawn" in json.dumps(d.get("SelectedProvider", {}))]
    if not dawn_conditions:
        r.warn("night-loop", "No dawn-bounded TimeCondition — night loop will "
                             "not know when to stop")

    tracking_modes = [t.get("TrackingMode") for t in _find_type(seq, "SetTracking")]
    if 0 not in tracking_modes:
        r.error("tracking", "No SetTracking sidereal (mode 0) instruction")
    if 5 not in tracking_modes:
        r.warn("tracking", "No SetTracking stopped (mode 5) — mount left tracking "
                           "at shutdown?")

    guide_elems = (_find_type(seq, "StartGuiding") + _find_type(seq, "StopGuiding")
                   + _find_type(seq, "DitherAfterExposures"))
    if guided:
        starts = _start_guiding_outside_cal(seq)
        if not starts:
            r.error("guiding", "Guided run but no StartGuiding instruction")
        elif (not starts[0].get("ForceCalibration", False) and _force_cal_wanted()
              and not _cal_containers(seq)):
            # PS-72: only an error when the config asks for a forced first
            # calibration; with PS_GUIDING_FORCE_FIRST_CALIBRATION=false (the
            # default since 2026-09-27) PHD2's saved calibration is trusted.
            r.error("guiding", "First StartGuiding must set ForceCalibration=true "
                               "(PS_GUIDING_FORCE_FIRST_CALIBRATION is on)")
        if not _has_type(seq, "StopGuiding"):
            r.warn("guiding", "Guided run without StopGuiding in shutdown")
        _check_selftest(seq, r)
        _check_calibration_slot(seq, r)
    else:
        # A StopGuiding in the shutdown is ALLOWED (and desirable) on an
        # unguided run — it harmlessly stops a stray looping PHD2.
        # A DitherAfterExposures trigger is now MANDATORY on every SmartExposure
        # (NINA's SmartExposure.Validate() indexes Triggers[0] and throws
        # ArgumentOutOfRangeException without it), so an unguided run carries it
        # with AfterExposures=0 — dithering disabled, no guider needed. Only an
        # ACTIVE dither (AfterExposures>0) or a StartGuiding is wrong here.
        bad = (_find_type(seq, "StartGuiding")
               + [d for d in _find_type(seq, "DitherAfterExposures")
                  if d.get("AfterExposures", 0) > 0 and not unguided_dither])
        if bad:
            kinds = sorted({d["$type"].split(",")[0].split(".")[-1] for d in bad})
            r.error("guiding", f"Unguided run contains guiding elements: {kinds}")

    # --- Per-target checks -------------------------------------------------
    # PS-26: the nested pier-West centering container of a Piggy-600-driven
    # target is a DeepSkyObjectContainer only so its Center inherits the
    # shifted coordinates; it is checked by _check_piggy_center, not here.
    _check_piggy_center(seq, r)
    # PS-148: the nested through-focus steps are checked with their test.
    targets = [(p, d) for p, d in _types_in(seq)
               if "DeepSkyObjectContainer" in d["$type"]
               and not _is_piggy_center(d) and not _is_optics_step(d)
               and not _is_tpoint_point(d)]
    if not targets:
        r.error("targets", "No DeepSkyObjectContainer targets found")

    for _path, tgt in targets:
        name = (tgt.get("Target") or {}).get("TargetName") or tgt.get("Name", "?")
        if " through-focus sweep" in json.dumps(tgt):   # PS-148
            _check_optics_test(
                tgt, name, any(" through-focus sweep" not in json.dumps(d)
                               for _p, d in targets), r)

        is_tpoint = _is_tpoint_mapping(tgt)   # PS-171
        if is_tpoint:
            _check_tpoint_mapping(tgt, name, any(
                not _is_tpoint_mapping(d) for _p, d in targets), r)

        cond_blob = json.dumps(tgt.get("Conditions", {}))
        if "SafetyMonitorCondition" not in cond_blob:
            r.error("safety", f"[{name}] missing SafetyMonitorCondition — scope "
                              "will keep shooting into clouds")
        if "AltitudeCondition" not in cond_blob and not is_tpoint:
            r.warn("altitude", f"[{name}] missing AltitudeCondition (want >=30 deg)")

        # An empty target still slews/AFs/centers; looping under Safety +
        # Altitude it re-acquires all night (2026-09-21, PS-27).
        # PS-76 focus-offset calibration target: no lights by design, a
        # bracketed series of AF runs instead. Allowed, with a note that the
        # night loop repeats it while the field is up and it is safe.
        is_focus_cal = (not (_has_type(tgt, "TakeExposure")
                             or _has_type(tgt, "SmartExposure"))
                        and "focus calibration AFs" in json.dumps(tgt)
                        and len(_find_type(tgt, "RunAutofocus")) >= 3)
        # PS-84 unguided tracking test: an exposure ladder with guiding
        # stopped. Allowed (WARN, so the operator knows what it is); any
        # StartGuiding or active dither inside it defeats the test.
        is_tracking_test = " unguided ladder" in json.dumps(tgt)
        if is_tracking_test:
            guiding = (_find_type(tgt, "StartGuiding")
                       + [d for d in _find_type(tgt, "DitherAfterExposures")
                          if d.get("AfterExposures", 0) > 0])
            if guiding:
                r.error("tracking-test", f"[{name}] unguided tracking test "
                        "contains StartGuiding or an active dither")
            if not _find_type(tgt, "StopGuiding"):
                r.warn("tracking-test", f"[{name}] tracking test does not "
                       "stop guiding first")
            # PS-127: a sideloaded test is followed by tonight's targets
            # (and runs once, before the night loop); only the standalone
            # download parks and holds after it.
            followed = any(" unguided ladder" not in json.dumps(d)
                           for _p, d in targets)
            after = ("Tonight's targets follow it; it runs once per night."
                     if followed else
                     "After the ladder the scope parks and holds until "
                     "dawn; stop the sequence to image.")
            r.warn("tracking-test", f"[{name}] unguided tracking test "
                   "(TPoint + ProTrack check): guiding stopped, no dithers. "
                   + after)
        if is_focus_cal:
            # PS-149: followed by other targets it runs its AF series once
            series = [d for d in _walk_dicts(tgt) if str(d.get("Name") or "")
                      .endswith("focus calibration AFs")]
            runs_once = any(_runs_once(c) for s in series
                            for c in _vals(s.get("Conditions")))
            how = ("it runs its AF series once, then tonight's targets follow"
                   if runs_once else "it repeats while safe and above the "
                   "altitude limit")
            r.warn("focus-calibration", f"[{name}] focus-offset calibration "
                   f"target (AF runs only, no lights); {how}. Turn off NINA's "
                   "profile Autofocus filter for this run.")
        elif not (_has_type(tgt, "TakeExposure") or _has_type(tgt, "SmartExposure")):
            r.error("empty-target", f"[{name}] has no exposures — it would "
                                    "slew, focus and center on every pass")
        elif "LoopCondition" not in cond_blob:
            r.warn("reacquire", f"[{name}] target container has no "
                                "LoopCondition(1) — it may re-slew/AF/center "
                                "on every pass")

        # Centering: Platesolving.Center or SlewScopeAndCenter both plate-solve
        if not (_has_type(tgt, "Platesolving.Center")
                or _has_type(tgt, "SlewScopeAndCenter")) and not is_tpoint:
            r.error("platesolve", f"[{name}] no plate-solve centering — blind slew "
                                  "can miss by arcminutes")

        # Filter switch BEFORE autofocus, in document order
        order = [d["$type"] for _, d in _types_in(tgt)]
        af_idx = next((i for i, t in enumerate(order) if "RunAutofocus" in t), None)
        sf_idx = next((i for i, t in enumerate(order) if "SwitchFilter" in t), None)
        if af_idx is None:
            r.warn("autofocus", f"[{name}] no RunAutofocus in target block")
        elif sf_idx is None or sf_idx > af_idx:
            r.error("filter-af", f"[{name}] SwitchFilter must come BEFORE "
                                 "RunAutofocus (AF runs through the capture filter)")

    return r


def lint_file(path: str, guided: bool | None = None) -> LintResult:
    with open(path, encoding="utf-8") as f:
        return lint(json.load(f), guided=guided)


def format_result(result: LintResult) -> str:
    if not result.findings:
        return "PASS — no findings"
    lines = [f"{f.level:5s} [{f.rule}] {f.detail}" for f in result.findings]
    n_err = sum(f.level == "ERROR" for f in result.findings)
    n_warn = sum(f.level == "WARN" for f in result.findings)
    lines.append(f"\n{'PASS' if result.ok else 'FAIL'} — {n_err} error(s), "
                 f"{n_warn} warning(s)")
    return "\n".join(lines)
