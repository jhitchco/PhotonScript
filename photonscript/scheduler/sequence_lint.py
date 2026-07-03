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


def lint(seq: dict, guided: bool | None = None) -> LintResult:
    """Validate a parsed sequence. guided=None auto-detects from content."""
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

    if not _has_type(seq, "MeridianFlipTrigger"):
        r.error("meridian", "No MeridianFlipTrigger found anywhere in sequence")

    tracking_modes = [t.get("TrackingMode") for t in _find_type(seq, "SetTracking")]
    if 0 not in tracking_modes:
        r.error("tracking", "No SetTracking sidereal (mode 0) instruction")
    if 5 not in tracking_modes:
        r.warn("tracking", "No SetTracking stopped (mode 5) — mount left tracking "
                           "at shutdown?")

    guide_elems = (_find_type(seq, "StartGuiding") + _find_type(seq, "StopGuiding")
                   + _find_type(seq, "DitherAfterExposures"))
    if guided:
        starts = _find_type(seq, "StartGuiding")
        if not starts:
            r.error("guiding", "Guided run but no StartGuiding instruction")
        elif not starts[0].get("ForceCalibration", False):
            r.error("guiding", "First StartGuiding must set ForceCalibration=true "
                               "(stale PHD2 calibration causes runaway errors)")
        if not _has_type(seq, "StopGuiding"):
            r.warn("guiding", "Guided run without StopGuiding in shutdown")
    else:
        if guide_elems:
            kinds = sorted({d["$type"].split(".")[-1].split(",")[0] for d in guide_elems})
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
