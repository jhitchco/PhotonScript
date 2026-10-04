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


def lint(seq: dict, guided: bool | None = None,
         unguided_dither: bool = False) -> LintResult:
    """Validate a parsed sequence. guided=None auto-detects from content.
    unguided_dither (PS-66): an unguided run may carry active dithers (NINA
    Direct Guider); StartGuiding is still an error."""
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
    targets = [(p, d) for p, d in _types_in(seq)
               if "DeepSkyObjectContainer" in d["$type"]]
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
            r.warn("tracking-test", f"[{name}] unguided tracking test "
                   "(TPoint + ProTrack check): guiding stopped, no dithers. "
                   "After the ladder the scope parks and holds until dawn; "
                   "stop the sequence to image.")
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
