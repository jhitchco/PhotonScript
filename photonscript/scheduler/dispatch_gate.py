"""Dispatch gate (PS-197 follow-up, 2026-10-15): lint every NINA #1 night
against the ARM before it is loaded.

Why: 2026-10-14 was armed UNGUIDED and planned as a deep night on NGC 604.
The NGC 604 container held a StartGuiding; the unguided guard had (by
design) disconnected the guider, so StartGuiding failed its 3 attempts and
SkipInstructionSetOnError skipped the container. The night loop then
parked, unparked, slewed, centered and focused on it again and again for
20 minutes with no lights. The armer's lint could not catch it: it was
told the mode the TARGETS carried (`any(t.start_guiding)`), so a guided
plan linted as a guided run.

The gate checks the generated JSON against the mode the night was ARMED
with (Armer._use_guiding, which the PS-156 fallback also sets):

  unguided  no StartGuiding anywhere; no ConnectEquipment(Guider) unless
            the night dithers through NINA's Direct Guider (PS-66
            unguided_dither: _guider_connect_item connects it on purpose)
  guided    every target the plan kept guided has a StartGuiding in its
            container (PS-85 per-block StartGuiding counts); a target the
            per-block decisions turned unguided is not required to
  always    at least one target container with exposure items (no empty
            night)

plus the existing sequence_lint ERRORs (passed in). check() returns the
verdict the armer records (run events kind "dispatch_gate", the Arm card
line) and acts on: ERROR refuses the load and the armer regenerates the
same goals in split mode once; when that is clean it loads it and says so,
else NINA #1 is left idle and the alert names the reason.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

KIND = "dispatch_gate"


def _cls(d: dict) -> str:
    return str(d.get("$type") or "").split(",")[0].strip()


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is(d: dict, cls_tail: str) -> bool:
    c = _cls(d)
    return c == cls_tail or c.endswith("." + cls_tail)


def start_guiding_items(tree) -> list[dict]:
    return [d for d in _walk(tree) if _is(d, "StartGuiding")]


def guider_connects(tree) -> list[dict]:
    return [d for d in _walk(tree) if _is(d, "ConnectEquipment")
            and "guider" in str(d.get("SelectedDevice") or "").lower()]


def _target_name(d: dict) -> str:
    return str((d.get("Target") or {}).get("TargetName") or d.get("Name") or "?")


def _has_lights(d: dict) -> bool:
    for x in _walk(d.get("Items")):
        if _is(x, "SmartExposure") or _is(x, "TakeExposure"):
            return True
    return False


def target_containers(tree) -> list[dict]:
    """The top-level DeepSkyObjectContainers (a nested DSO container, e.g.
    the PS-26 pier-West center or a through-focus step, belongs to its
    parent target)."""
    out = []

    def visit(node, inside: bool):
        if isinstance(node, dict):
            is_dso = _is(node, "DeepSkyObjectContainer")
            if is_dso and not inside:
                out.append(node)
            for v in node.values():
                visit(v, inside or is_dso)
        elif isinstance(node, list):
            for v in node:
                visit(v, inside)

    visit(tree, False)
    return out


def check(seq: dict, *, guided: bool, unguided_dither: bool = False,
          guided_targets=None, lint_errors=None, mode: str | None = None) -> dict:
    """The verdict for one generated night (see module doc). guided: the
    ARM's mode. guided_targets: names of the targets the plan kept guided
    (None = every target container when guided). lint_errors: the
    sequence_lint ERROR texts for the same JSON. mode: the planner mode
    (split | deep), for the record. Returns {"ok", "level", "errors",
    "notes", "guided", "mode", "targets", "lights_targets",
    "start_guiding", "guider_connect", "line"}. Never raises."""
    errors: list[str] = []
    notes: list[str] = []
    try:
        starts = start_guiding_items(seq)
        connects = guider_connects(seq)
        tcs = target_containers(seq)
        with_lights = [c for c in tcs if _has_lights(c)]
        if not tcs:
            errors.append("empty night: no target container")
        elif not with_lights:
            errors.append("no target container has exposure items ("
                          + ", ".join(sorted({_target_name(c) for c in tcs})) + ")")
        if not guided:
            if starts:
                where = sorted({_target_name(c) for c in tcs
                                if start_guiding_items(c)})
                errors.append(f"armed UNGUIDED but the sequence has "
                              f"{len(starts)} StartGuiding"
                              + (f" (in {', '.join(where)})" if where else
                                 " (outside the targets)"))
            if connects:
                if unguided_dither:
                    notes.append("guider connected for Direct Guider dithers "
                                 "(unguided_dither)")
                else:
                    errors.append("armed UNGUIDED but the Start area connects "
                                  "the guider")
        else:
            want = (None if guided_targets is None
                    else {str(n) for n in guided_targets})
            missing = []
            for c in with_lights:
                name = _target_name(c)
                if want is not None and name not in want:
                    continue
                if not start_guiding_items(c):
                    missing.append(name)
            if missing:
                errors.append("armed GUIDED but no StartGuiding in "
                              + ", ".join(sorted(set(missing))))
            if not connects:
                notes.append("guided night without ConnectEquipment(Guider)")
        for e in lint_errors or []:
            errors.append(f"lint: {e}")
    except Exception as e:  # noqa: BLE001 - a broken gate refuses
        logger.warning("dispatch gate failed: %s", e)
        errors.append(f"gate could not read the sequence: {e}")
        starts, connects, tcs, with_lights = [], [], [], []
    ok = not errors
    line = (f"{'OK' if ok else 'ERROR'} ({'guided' if guided else 'unguided'}"
            f"{', ' + mode if mode else ''}): "
            + (f"{len(with_lights)} target(s) with lights, "
               f"{len(starts)} StartGuiding" if ok else "; ".join(errors)))
    return {"ok": ok, "level": "OK" if ok else "ERROR", "errors": errors,
            "notes": notes, "guided": bool(guided), "mode": mode,
            "targets": len(tcs), "lights_targets": len(with_lights),
            "start_guiding": len(starts), "guider_connect": bool(connects),
            "line": line}


def check_text(content: str, **kw) -> dict:
    try:
        seq = json.loads(content)
    except (TypeError, ValueError) as e:
        return check({}, lint_errors=[f"not JSON: {e}"], **kw)
    return check(seq, **kw)
