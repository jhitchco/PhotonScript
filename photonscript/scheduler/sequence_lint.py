"""Sequence linter — validates NINA Advanced Sequencer JSON before dispatch.

Encodes hard-won AARO operational rules. A sequence must pass with zero
errors before it is sent to the telescope. Catches the failure modes that
otherwise surface at 3 AM with nobody watching.
"""

from __future__ import annotations

import json
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


PIGGY_CENTER_TAG = "Piggy-600 centering (PS-26)"


def _is_piggy_center(d: dict) -> bool:
    from photonscript.scheduler.nina_sequence_json import PIGGY_WEST_CENTER_SUFFIX
    return str(d.get("Name") or "").endswith(PIGGY_WEST_CENTER_SUFFIX)


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
         settle_gate: bool | None = None) -> LintResult:
    """Validate a parsed sequence. guided=None auto-detects from content.
    unguided_dither (PS-66): an unguided run may carry active dithers (NINA
    Direct Guider); StartGuiding is still an error. cooler_gate (PS-61):
    require the gate before every light loop (None = from the config).
    filter_wheel (PS-132): False for a rig without a wheel.
    settle_gate (PS-27): require the settle gate before every OSC light in
    the Piggy-600 light loop (None = from the config)."""
    r = LintResult()

    if guided is None:
        guided = _has_type(seq, "StartGuiding")

    # --- Global checks -----------------------------------------------------
    cools = _find_type(seq, "CoolCamera")
    if not cools:
        r.warn("cooling", "No CoolCamera instruction found")
    for c in cools:
        temp = c.get("Temperature")
        if temp is None or temp > 0.0:
            r.error("cooling", f"CoolCamera Temperature is {temp!r} — must be at "
                               "or below the 0.0°C setpoint (never a warm sensor)")

    _check_focus_moves(seq, r)
    _check_parent_links(seq, r)
    _check_light_loop_guards(seq, r)
    _check_cooler_gate(seq, r, cooler_gate)
    _check_settle_gate(seq, r, settle_gate)
    _check_readout_mode(seq, r)
    _check_flat_filters(seq, r, filter_wheel)   # PS-132

    if not _has_type(seq, "MeridianFlipTrigger"):
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
    targets = [(p, d) for p, d in _types_in(seq)
               if "DeepSkyObjectContainer" in d["$type"]
               and not _is_piggy_center(d)]
    if not targets:
        r.error("targets", "No DeepSkyObjectContainer targets found")

    for _path, tgt in targets:
        name = (tgt.get("Target") or {}).get("TargetName") or tgt.get("Name", "?")

        cond_blob = json.dumps(tgt.get("Conditions", {}))
        if "SafetyMonitorCondition" not in cond_blob:
            r.error("safety", f"[{name}] missing SafetyMonitorCondition — scope "
                              "will keep shooting into clouds")
        if "AltitudeCondition" not in cond_blob:
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
            r.warn("focus-calibration", f"[{name}] focus-offset calibration "
                   "target (AF runs only, no lights); it repeats while safe "
                   "and above the altitude limit. Turn off NINA's profile "
                   "Autofocus filter for this run.")
        elif not (_has_type(tgt, "TakeExposure") or _has_type(tgt, "SmartExposure")):
            r.error("empty-target", f"[{name}] has no exposures — it would "
                                    "slew, focus and center on every pass")
        elif "LoopCondition" not in cond_blob:
            r.warn("reacquire", f"[{name}] target container has no "
                                "LoopCondition(1) — it may re-slew/AF/center "
                                "on every pass")

        # Centering: Platesolving.Center or SlewScopeAndCenter both plate-solve
        if not (_has_type(tgt, "Platesolving.Center")
                or _has_type(tgt, "SlewScopeAndCenter")):
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
