"""PS-123: sideload a hand-built night into NINA #1 / NINA #2 (load, never start).

On 2026-10-05 tonight's sequence (a tracking test on M 2, then tonight's
targets minus Cat's Eye) and the Piggy-600 companion were spliced on the
desktop and loaded by hand over ninaAPI. This module is that procedure as
pure functions, plus two tiny ninaAPI calls:

  splice_tracking_test()  tracking-test DeepSkyObjectContainer in the
                          Targets area BEFORE the night loop (PS-127: runs
                          once per night; with the re-entry gate script,
                          first in the night loop behind its
                          tracking_test gate instead, so a pause before
                          the ladder is done runs it again), then
                          tonight's targets minus
                          `exclude` in tonight's own start / night loop /
                          shutdown; ids + Parent links rebuilt with
                          link_parents() (PS-77)
  splice_optics_test()    PS-148: the same splice for the through-focus
                          optics test (recipe optics_through_focus)
  splice_tpoint_mapping() PS-171: the same splice for the TPoint mapping
                          run (recipe tpoint_mapping_then_tonight)
  splice_guider_commission()  2026-10-28: the same splice for the guider
                          commissioning night (recipe guider_commission),
                          plus the guide-star check inside tonight's
                          PHD2_CALIBRATION slot (scheduler/guider_night.py)
  lint_companion()        the Piggy companion's own lint (the RC16 night lint
                          wants a mount, targets and a meridian flip, which a
                          camera-only companion never has)
  nina_running()          RUNNING items in a ninaAPI sequence tree
  read_sequence_state()   GET /sequence/state (falls back to /sequence/json)
  load_only()             POST /sequence/load. Never /sequence/stop, never
                          /sequence/start: Start stays in NINA.

The endpoints live in scheduler/routers/sideload.py.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

RECIPE_TT_THEN_TONIGHT = "tracking_test_then_tonight"
RECIPE_OPTICS_THROUGH_FOCUS = "optics_through_focus"   # PS-148
RECIPE_TPOINT_MAPPING = "tpoint_mapping_then_tonight"   # PS-171
RECIPE_GUIDER_COMMISSION = "guider_commission"         # 2026-10-28
RECIPES = {
    RECIPE_TT_THEN_TONIGHT: (
        "RC16: tracking test on an auto-picked field, then tonight's targets "
        "(minus any excluded). Piggy-600: the companion with lights."),
    RECIPE_OPTICS_THROUGH_FOCUS: (
        "RC16: through-focus optics test (astigmatism / collimation) on an "
        "auto-picked field, then tonight's targets (minus any excluded). "
        "Piggy-600: the companion with lights."),
    RECIPE_TPOINT_MAPPING: (
        "RC16: TPoint mapping run (N blind alt/az points, Image Link each "
        "through TheSky, no center, no sync), then tonight's targets (minus "
        "any excluded). Piggy-600: the companion with lights."),
    RECIPE_GUIDER_COMMISSION: (
        "RC16, guided: dark library and guide exposure checks before dusk, "
        "a guide-star check and the PS-93 calibration in the calibration "
        "slot, the PS-92 pulse self-test, 5 min of guiding for the "
        "min-move, the narrowband focus test (AF per filter, Ha sweep), then "
        "tonight's targets with the guiding test in act. Piggy-600: the "
        "companion with lights."),
}

# PS-181: an arm may carry one of these recipes (POST /api/arm {"recipe",
# "recipe_opts"}); the armer runs it at pre-config instead of a hand Start.
# Short labels for the Arm select and the armer card.
RECIPE_LABELS = {
    RECIPE_TPOINT_MAPPING: "TPoint mapping",
    RECIPE_TT_THEN_TONIGHT: "tracking test",
    RECIPE_OPTICS_THROUGH_FOCUS: "optics test",
    RECIPE_GUIDER_COMMISSION: "guider commissioning",
}
ADD_MODES = ("off", "auto")   # tpoint_sample.ADD_MODES


TT_OPTS = ("exposures", "filters", "repeats", "fields")


def _csv(v) -> list:
    if isinstance(v, str):
        return [x.strip() for x in v.split(",") if x.strip()]
    return list(v) if isinstance(v, (list, tuple)) else [v]


def tracking_test_opts(opts: dict) -> tuple[dict | None, str | None]:
    """2026-11-11 drift ladder: the tracking-test recipe's options.
    exposures (s, 1..3600), filters (names), repeats (1..10), fields (list of
    {"label", "ha", "dec"} or {"label", "ra", "dec"}, at most 6; see
    tracking_test.place_fields). Unknown keys are an error."""
    from photonscript.scheduler.tracking_test import MAX_FIELDS
    bad = sorted(set(opts) - set(TT_OPTS))
    if bad:
        return None, (f"recipe_opts {bad} not allowed for {RECIPE_TT_THEN_TONIGHT}"
                      f" (allowed: {', '.join(TT_OPTS)})")
    out: dict = {}
    try:
        if "exposures" in opts:
            ex = sorted({float(x) for x in _csv(opts["exposures"])})
            if not ex or ex[0] <= 0 or ex[-1] > 3600:
                return None, "exposures must be 1 to 3600 s"
            out["exposures"] = ex
        if "filters" in opts:
            out["filters"] = [str(x) for x in _csv(opts["filters"])]
        if "repeats" in opts:
            out["repeats"] = max(1, min(int(opts["repeats"]), 10))
        if "fields" in opts:
            fl = opts["fields"]
            if not isinstance(fl, list) or not fl or len(fl) > MAX_FIELDS:
                return None, f"fields must be a list of 1 to {MAX_FIELDS} objects"
            clean = []
            for i, f in enumerate(fl):
                if not isinstance(f, dict):
                    return None, "each field must be an object"
                c = {"label": str(f.get("label") or f"F{i + 1}")[:24]}
                for k in ("ha", "ra", "dec"):
                    if f.get(k) is not None:
                        c[k] = float(f[k])
                if "ha" not in c and not ("ra" in c and "dec" in c):
                    return None, "each field needs ha, or ra and dec"
                clean.append(c)
            out["fields"] = clean
    except (TypeError, ValueError) as e:
        return None, f"bad tracking-test option: {e}"
    label = RECIPE_LABELS[RECIPE_TT_THEN_TONIGHT]
    bits = []
    if out.get("exposures"):
        bits.append(f"{out['exposures'][0]:g} to {out['exposures'][-1]:g} s")
    if out.get("fields"):
        bits.append(f"{len(out['fields'])} fields")
    if bits:
        label += f" ({', '.join(bits)})"
    return {"id": RECIPE_TT_THEN_TONIGHT, "label": label, "opts": out}, None


def arm_recipe(recipe, opts=None) -> tuple[dict | None, str | None]:
    """PS-181: validate an arm's recipe. (None, None) for no recipe,
    ({"id", "label", "opts"}, None) when valid, (None, error) otherwise.
    Options: TPoint mapping only, "points" (clamped to the mapping's
    MIN_POINTS..MAX_POINTS) and "add" ("off" | "auto", tpoint_sample_add
    for this run); guider commissioning: "skip" (step ids), "rounds",
    "star_tries" (guider_night.parse_opts). Anything else is an error, not
    silently dropped."""
    rid = str(recipe or "").strip()
    if not rid or rid.lower() == "none":
        return None, None
    if rid not in RECIPES:
        return None, (f"unknown recipe {rid!r}: use one of "
                      + ", ".join(sorted(RECIPES)))
    if opts is not None and not isinstance(opts, dict):
        return None, "recipe_opts must be an object"
    if rid == RECIPE_GUIDER_COMMISSION:
        from photonscript.scheduler.guider_night import parse_opts
        gopts, err = parse_opts(opts)
        if err:
            return None, err
        label = RECIPE_LABELS[rid] + (f" (skip {', '.join(gopts['skip'])})"
                                      if gopts.get("skip") else "")
        return {"id": rid, "label": label, "opts": gopts}, None
    opts = {k: v for k, v in (opts or {}).items() if v not in (None, "")}
    if rid == RECIPE_TT_THEN_TONIGHT and opts:
        return tracking_test_opts(opts)
    allowed = ("points", "add") if rid == RECIPE_TPOINT_MAPPING else ()
    bad = sorted(set(opts) - set(allowed))
    if bad:
        return None, (f"recipe_opts {bad} not allowed for {rid}"
                      + (f" (allowed: {', '.join(allowed)})" if allowed else ""))
    out: dict = {}
    if "points" in opts:
        from photonscript.scheduler.tpoint_mapping import clamp_points
        try:
            out["points"] = clamp_points(int(opts["points"]))
        except (TypeError, ValueError):
            return None, f"points must be a whole number, not {opts['points']!r}"
    if "add" in opts:
        add = str(opts["add"]).strip().lower()
        if add not in ADD_MODES:
            return None, f"add must be off or auto, not {opts['add']!r}"
        out["add"] = add
    label = RECIPE_LABELS.get(rid, rid)
    if rid == RECIPE_TPOINT_MAPPING and "points" in out:
        label += f" ({out['points']})"
    return {"id": rid, "label": label, "opts": out}, None


TARGETS_CONTAINER = "TARGETS_CONTAINER"
TARGET_AREA = "TargetAreaContainer"
NIGHT_LOOP = "LOOP_ALL_NIGHT"
DSO = "DeepSkyObjectContainer"

# PS-127: replaces the standalone download's park-and-hold sentence in the
# spliced test's annotation (tonight's targets follow it here).
SPLICED_TT_NOTE = ("Sideloaded (PS-127): this test runs once, in the Targets "
                   "area before the night loop, then tonight's targets "
                   "follow. An unsafe spell during the ladder ends the test "
                   "for tonight; after an unsafe pause the night loop never "
                   "runs it again.")
# PS-127 (2026-11-06): the gated splice (the test first in the night loop).
SPLICED_TT_GATED_NOTE = ("Sideloaded (PS-127): this test runs first in the "
                         "night loop, then tonight's targets follow. Its gate "
                         "skips it once tonight has the whole ladder (or it "
                         "started twice); a pause before that runs it again "
                         "on resume.")
# PS-171: appended to the TPoint mapping run's annotation when spliced.
SPLICED_TPM_NOTE = ("Sideloaded (PS-171): this run goes once, in the Targets "
                    "area before the night loop, then tonight's targets "
                    "follow (each with its own slew, AF and center).")
# PS-148: the same for the through-focus optics test.
SPLICED_OT_NOTE = ("Sideloaded (PS-148): this test runs once, in the Targets "
                   "area before the night loop, then tonight's targets "
                   "follow (each starts with its own autofocus).")

# Instructions a camera-only companion must never carry: the Piggy-600 rides
# the RC16 mount, which NINA #1 owns. PS-139: the mount list proper lives in
# sequence_lint.MOUNT_CLASSES (rule piggy-mount); GUIDER_TYPES stay an
# ERROR anywhere (companion-mount), hand-built or not.
MOUNT_TYPES = ("SlewScope", "ParkScope", "UnparkScope", "Platesolving.Center",
               "SetTracking", "MeridianFlip", "StartGuiding")
GUIDER_TYPES = ("StartGuiding",)


class SpliceError(ValueError):
    """The input sequences do not have the shape the splice expects."""


# --------------------------------------------------------------- tree helpers

def _short(t: str) -> str:
    return (t or "").split(",")[0].split(".")[-1]


def _items(node: dict) -> list:
    it = node.get("Items")
    if isinstance(it, dict):
        return it.get("$values") or []
    if isinstance(it, list):   # ninaAPI /sequence/json shape
        return it
    return []


def strip_ids(node):
    """A copy without any "$id" / "Parent" keys (link_parents re-adds them)."""
    if isinstance(node, dict):
        return {k: strip_ids(v) for k, v in node.items()
                if k not in ("$id", "Parent")}
    if isinstance(node, list):
        return [strip_ids(x) for x in node]
    return node


def non_parent_refs(node, key=None, out=None) -> list:
    """Keys that hold a {"$ref"} anywhere other than "Parent". A splice drops
    every $id, so such a reference would dangle; the generators emit none."""
    out = [] if out is None else out
    if isinstance(node, dict):
        if "$ref" in node and key != "Parent":
            out.append(key)
        for k, v in node.items():
            non_parent_refs(v, k, out)
    elif isinstance(node, list):
        for x in node:
            non_parent_refs(x, key, out)
    return out


def find_container(node, name: str, type_fragment: str):
    """First dict (depth first) with Name == name and $type containing
    type_fragment, else None."""
    if isinstance(node, dict):
        if node.get("Name") == name and type_fragment in (node.get("$type") or ""):
            return node
        for v in node.values():
            r = find_container(v, name, type_fragment)
            if r is not None:
                return r
    elif isinstance(node, list):
        for x in node:
            r = find_container(x, name, type_fragment)
            if r is not None:
                return r
    return None


def _targets_container(seq: dict) -> dict:
    tc = find_container(seq, TARGETS_CONTAINER, "SequentialContainer")
    if tc is None or not isinstance(tc.get("Items"), dict):
        raise SpliceError(f"no {TARGETS_CONTAINER} in {seq.get('Name')!r}")
    return tc


def _is_dso(v) -> bool:
    return isinstance(v, dict) and DSO in (v.get("$type") or "")


def target_names(seq: dict) -> list[str]:
    """DeepSkyObjectContainer names in run order: any directly in the
    Targets area (a spliced tracking test, PS-127), then TARGETS_CONTAINER's."""
    try:
        tc = _targets_container(seq)
    except SpliceError:
        return []
    area = find_container(seq, "Targets", TARGET_AREA)
    first = _items(area) if area else []
    return [str(v.get("Name")) for v in first + _items(tc) if _is_dso(v)]


def container_tree(seq: dict, max_depth: int = 4) -> list[dict]:
    """Containers as a flat outline: [{"depth", "type", "name"}], document
    order, down to max_depth (the dashboard indents by depth)."""
    out: list[dict] = []

    def walk(n, d):
        if not isinstance(n, dict):
            return
        t = _short(n.get("$type", ""))
        if "Container" in t:
            out.append({"depth": d, "type": t, "name": n.get("Name")})
            if d >= max_depth:
                return
            for ch in _items(n):
                walk(ch, d + 1)

    walk(seq, 0)
    return out


# ---------------------------------------------------------------------- splice

def _norm(s) -> str:
    return " ".join(str(s or "").split()).lower()


def splice_tracking_test(night_seq: dict, tt_seq: dict, exclude=(),
                         name: str | None = None,
                         gate_script: str | None = None) -> dict:
    """Tonight's sequence with the tracking test's DeepSkyObjectContainer as
    the first item of the Targets area, BEFORE LOOP_ALL_NIGHT, and tonight's
    targets not in `exclude` left in TARGETS_CONTAINER in tonight's order.
    Everything else (start area with the dusk wait, night loop, unsafe darks,
    shutdown, dawn flats) is tonight's. The tracking test's own park-and-hold
    (after its TARGETS_CONTAINER) is not taken, and its annotation says so.

    PS-127: inside the night loop the test's LoopCondition(1) was reset each
    time LOOP_ALL_NIGHT looped after an unsafe pause, so the ladder ran again
    (about 1 h). The Targets area runs once and is never reset: the test runs
    at most once per night. Unsafe during the ladder: its Safety conditions
    end the test (rest of the ladder skipped), the night loop then parks in
    its UNSAFE branch and resumes tonight's targets. Unsafe when the Targets
    area starts: the test is skipped.

    2026-11-05 that missed the test: the roof closed during its opening AF,
    the night loop resumed tonight's targets and the test never ran. With
    `gate_script` (the re-entry gate .cmd, tracking_test.gate_script) the
    test goes FIRST in TARGETS_CONTAINER instead, inside the night loop, so
    every resume reaches it, and its first item is the tracking_test gate
    (ErrorBehavior 1): SKIP once tonight's runs log holds the whole ladder
    or the test started twice, so a pause after the ladder never reruns it
    and a pause before it does. Without a script: the Targets area as above.

    Names in `exclude` match case- and whitespace-insensitively. Returns a
    new tree with fresh $id / Parent links; inputs are untouched. Raises
    SpliceError on an unexpected shape."""
    from photonscript.scheduler.nina_sequence_json import TRACKING_TEST_PARK_NOTE
    if not gate_script:
        return splice_test(night_seq, tt_seq, exclude, name,
                           note=(TRACKING_TEST_PARK_NOTE, SPLICED_TT_NOTE),
                           what="tracking-test")
    return splice_test(night_seq, tt_seq, exclude, name,
                       note=(TRACKING_TEST_PARK_NOTE, SPLICED_TT_GATED_NOTE),
                       what="tracking-test", gate_script=gate_script)


def splice_optics_test(night_seq: dict, ot_seq: dict, exclude=(),
                       name: str | None = None) -> dict:
    """PS-148: splice_tracking_test for the through-focus optics test: the
    test's DeepSkyObjectContainer runs once in the Targets area before the
    night loop, then tonight's targets (minus `exclude`)."""
    from photonscript.scheduler.nina_sequence_json import OPTICS_TEST_PARK_NOTE
    return splice_test(night_seq, ot_seq, exclude, name,
                       note=(OPTICS_TEST_PARK_NOTE, SPLICED_OT_NOTE),
                       what="optics-test")


def splice_tpoint_mapping(night_seq: dict, tpm_seq: dict, exclude=(),
                          name: str | None = None) -> dict:
    """PS-171: splice_tracking_test for the TPoint mapping run: it runs once
    in the Targets area before the night loop, then tonight's targets
    (minus `exclude`)."""
    from photonscript.scheduler.nina_sequence_json import TPOINT_MAPPING_NOTE
    return splice_test(night_seq, tpm_seq, exclude, name,
                       note=(TPOINT_MAPPING_NOTE,
                             TPOINT_MAPPING_NOTE + " " + SPLICED_TPM_NOTE),
                       what="tpoint-mapping")


def splice_guider_commission(night_seq: dict, gc_seq: dict, exclude=(),
                             name: str | None = None,
                             star_item: dict | None = None) -> dict:
    """2026-10-28: splice_tracking_test for the guider commissioning night
    (the recipe container runs once before the night loop). star_item: the
    guide-star check (a guider-commission.cmd ExternalScript) placed in
    tonight's PHD2_CALIBRATION slot right after its Center, so a missing
    guide star stops the calibration before it starts (the item's
    SkipInstructionSetOnError). Raises SpliceError when star_item is given
    and tonight has no slot with a Center."""
    from photonscript.scheduler.nina_sequence_json import (
        GUIDER_COMMISSION_NOTE, link_parents)
    seq = splice_test(night_seq, gc_seq, exclude, name,
                      note=(GUIDER_COMMISSION_NOTE, GUIDER_COMMISSION_NOTE),
                      what="guider-commission")
    if star_item is None:
        return seq
    seq = strip_ids(seq)
    cal = calibration_slot(seq)
    vals = (cal.get("Items") or {}).get("$values") if cal else None
    at = next((i for i, v in enumerate(vals or [])
               if isinstance(v, dict)
               and "Platesolving.Center" in str(v.get("$type") or "")), None)
    if at is None:
        raise SpliceError(f"no Center in a PHD2 calibration slot of "
                          f"{night_seq.get('Name')!r} for the guide-star check")
    vals.insert(at + 1, strip_ids(star_item))
    return link_parents(seq)


def calibration_slot(seq: dict) -> dict | None:
    """Tonight's PS-93 PHD2_CALIBRATION container, or None."""
    from photonscript.scheduler.phd2_calibration import CONTAINER_NAME
    return find_container(seq, CONTAINER_NAME, "SequentialContainer")


def splice_test(night_seq: dict, test_seq: dict, exclude=(),
                name: str | None = None, note: tuple | None = None,
                what: str = "test", gate_script: str | None = None) -> dict:
    """The splice behind splice_tracking_test / splice_optics_test: the one
    DeepSkyObjectContainer of `test_seq` goes first in tonight's Targets
    area, before LOOP_ALL_NIGHT; `note` = (standalone sentence, spliced
    sentence) swapped in the test's annotation. gate_script (PS-127, the
    tracking test only): the test goes first in TARGETS_CONTAINER instead,
    led by its once-per-night gate."""
    from photonscript.scheduler.nina_sequence_json import link_parents
    bad = set(non_parent_refs(night_seq)) | set(non_parent_refs(test_seq))
    if bad:
        raise SpliceError(f"$ref outside Parent ({sorted(map(str, bad))}): "
                          "the splice would leave it dangling")
    night, tt = strip_ids(night_seq), strip_ids(test_seq)
    tests = [v for v in _items(_targets_container(tt)) if _is_dso(v)]
    # 2026-11-11: the tracking test may carry several fields (drift ladder
    # at several sky positions), each its own container and its own gate
    from photonscript.scheduler.tracking_test import is_tracking_test
    multi = what == "tracking-test" and all(
        is_tracking_test(t.get("Name")) for t in tests)
    if not tests or (len(tests) != 1 and not multi):
        raise SpliceError(f"{what} sequence has {len(tests)} targets, "
                          "want exactly 1")
    drop = {_norm(x) for x in (exclude or ()) if str(x or "").strip()}
    tc = _targets_container(night)
    kept = [v for v in _items(tc)
            if not (_is_dso(v) and _norm(v.get("Name")) in drop)]
    tc["Items"]["$values"] = kept
    area = find_container(night, "Targets", TARGET_AREA)
    vals = (area.get("Items") or {}).get("$values") if area else None
    loop_at = next((i for i, v in enumerate(vals or [])
                    if isinstance(v, dict) and v.get("Name") == NIGHT_LOOP),
                   None)
    if loop_at is None:
        raise SpliceError(f"no {NIGHT_LOOP} in the Targets area of "
                          f"{night_seq.get('Name')!r}")
    spliced = [_spliced_note(t, note) for t in tests]
    if gate_script:
        for test in spliced:
            test["Items"]["$values"].insert(0, _tt_gate(test, gate_script))
        tc["Items"]["$values"] = spliced + kept
    else:
        vals[loop_at:loop_at] = spliced
    if name:
        night["Name"] = name
    return link_parents(night)


def planned_subs(test: dict) -> int:
    """Lights in one pass of a test container: the LoopCondition iterations
    of every SmartExposure in it (0 when there is none)."""
    n = 0
    for d in _walk(test):
        if "SmartExposure" not in str(d.get("$type") or ""):
            continue
        for c in _items({"Items": d.get("Conditions")}):
            if "LoopCondition" in str(c.get("$type") or ""):
                n += int(c.get("Iterations") or 0)
                break
    return n


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _tt_gate(test: dict, script: str) -> dict:
    """PS-127: the tracking_test gate ExternalScript (ErrorBehavior 1, so a
    SKIP skips the whole test container)."""
    from photonscript.scheduler.nina_sequence_json import _external_script
    from photonscript.scheduler.tracking_test import gate_args
    from photonscript.scheduler.reentry import GATE_ERROR_BEHAVIOR
    g = _external_script(script, gate_args(test.get("Name"),
                                           planned_subs(test)))
    g["ErrorBehavior"] = GATE_ERROR_BEHAVIOR
    return g


def _spliced_note(test: dict, note: tuple | None = None) -> dict:
    """The test container with the standalone park-and-hold sentence in its
    annotation replaced by the spliced one (default: the tracking test's
    SPLICED_TT_NOTE). The copy is already ours."""
    if note is None:
        from photonscript.scheduler.nina_sequence_json import (
            TRACKING_TEST_PARK_NOTE)
        note = (TRACKING_TEST_PARK_NOTE, SPLICED_TT_NOTE)
    old, new = note
    for v in _items(test):
        text = v.get("Text") if isinstance(v, dict) else None
        if isinstance(text, str) and old in text:
            v["Text"] = text.replace(old, new)
    return test


def splice_name(date: str, field: str, kept: list[str], tag: str = "TT") -> str:
    """PhotonScript_<yyyymmdd>_<tag>_<field>_then_<target | tonight>
    (tag TT = tracking test, OT = optics test, PS-148, TPM = TPoint
    mapping, PS-171, GC = guider commissioning)."""
    def safe(s):
        return "_".join("".join(c if c.isalnum() else " " for c in str(s)).split())
    tail = safe(kept[0]) if len(kept) == 1 else ("tonight" if kept else "nothing")
    return (f"PhotonScript_{date.replace('-', '')}_{tag}_{safe(field)}"
            f"_then_{tail}")


# ------------------------------------------------------------------------ lint

def lint_companion(seq: dict, filter_wheel: bool | None = None,
                   settle_gate: bool | None = None, hand_built: bool = False,
                   cover: bool | None = None):
    """Lint for the Piggy-600 companion (camera + focuser only). Errors: a
    warm or missing setpoint, missing $id / Parent links (PS-77), a light
    loop without its own Safety + Time condition, a loop with no waiting
    item that can busy-spin (PS-149), a stale focus seed, any
    mount / guiding instruction, or (PS-132) a flat without its SwitchFilter
    or a SwitchFilter selecting a filter on a rig without a wheel.
    filter_wheel None = the Piggy-600's (rigs.rig_has_filter_wheel).
    settle_gate (PS-27): the settle-gate rule for the OSC light loop (None =
    from the config, as in sequence_lint.lint).
    hand_built (PS-139): a sequence someone built in NINA and sideloads as a
    JSON body. Rule piggy-mount: a mount instruction inside a loop or a
    trigger is an ERROR, one outside any loop a WARN (center once before
    the loop, never while the RC16 images). PhotonScript's own companion
    (hand_built False) never carries one: any is an ERROR, as before.
    cover (PS-189): the Wanderer Cover rules (cover-open, cover-flats,
    cover-end; None = from piggyback_cover_enabled, and always when the
    sequence carries a cover instruction).
    Returns a sequence_lint.LintResult."""
    from photonscript.scheduler import sequence_lint as sl
    from photonscript.shared.rigs import PIGGYBACK, rig_has_filter_wheel
    if filter_wheel is None:
        filter_wheel = rig_has_filter_wheel(PIGGYBACK)
    r = sl.LintResult()
    # PS-154: the Piggy-600 setpoint, bounded cool (a WARN when hand built)
    sl.check_cooling(seq, r, rig=PIGGYBACK, strict=not hand_built)
    sl._check_focus_moves(seq, r)
    sl._check_af_hfr(seq, r, hand_built)   # HFR-rise AF trigger floors
    sl._check_parent_links(seq, r)
    sl._check_light_loop_guards(seq, r)
    sl._check_loop_spin(seq, r)   # PS-149
    sl._check_readout_mode(seq, r)   # PS-128
    sl._check_flat_filters(seq, r, filter_wheel)   # PS-132
    sl._check_settle_gate(seq, r, settle_gate)   # PS-27
    sl._check_piggy_mount(seq, r, strict=not hand_built)   # PS-139
    sl._check_cover(seq, r, cover)   # PS-189
    sl._check_reentry_gates(seq, r)   # PS-241: the Piggy AF back-off gate
    mount = sorted({_short(d["$type"])
                    for frag in (GUIDER_TYPES if hand_built else MOUNT_TYPES)
                    for d in sl._find_type(seq, frag)})
    if mount:
        r.error("companion-mount", f"companion moves the shared mount or guider "
                                   f"({', '.join(mount)}); NINA #1 owns them")
    return r


def lint_dict(result) -> dict:
    """A LintResult as JSON: ok, counts, findings, the text report."""
    from photonscript.scheduler.sequence_lint import format_result
    f = [{"level": x.level, "rule": x.rule, "detail": x.detail}
         for x in result.findings]
    return {"ok": result.ok,
            "errors": sum(x["level"] == "ERROR" for x in f),
            "warnings": sum(x["level"] == "WARN" for x in f),
            "findings": f, "text": format_result(result)}


# --------------------------------------------------------------- NINA state

def _state_payload(data):
    if isinstance(data, dict) and "Response" in data:
        return data.get("Response")
    return data


def nina_running(state) -> list[str]:
    """Names of every RUNNING item in a ninaAPI sequence tree (the
    /sequence/state or /sequence/json payload, with or without the
    {"Response": ...} envelope; Items as a list or as {"$values": [...]}).
    Empty when nothing runs."""
    out: list[str] = []

    def walk(n):
        if isinstance(n, list):
            for x in n:
                walk(x)
            return
        if not isinstance(n, dict):
            return
        if str(n.get("Status", "")).upper() == "RUNNING":
            out.append(str(n.get("Name") or _short(n.get("$type", "")) or "?"))
        for key in ("Items", "Conditions", "Triggers"):
            v = n.get(key)
            walk(v.get("$values") if isinstance(v, dict) else v)

    walk(_state_payload(state))
    return out


async def read_sequence_state(base_url: str, client=None):
    """(tree, error) from NINA's sequence state: GET /sequence/state, or
    /sequence/json on plugin builds without it (404). tree is None and error
    says why when NINA cannot be read; callers treat that as "not idle"."""
    base = base_url.rstrip("/")
    own = client is None
    client = client or httpx.AsyncClient(timeout=20)
    try:
        last = "no endpoint answered"
        for path in ("/sequence/state", "/sequence/json"):
            try:
                r = await client.get(base + path)
            except Exception as e:  # noqa: BLE001
                return None, f"{type(e).__name__}: {e}"
            if r.status_code == 404:
                last = f"{path}: 404"
                continue
            if r.status_code >= 400:
                return None, f"{path}: HTTP {r.status_code}"
            try:
                data = r.json()
            except ValueError:
                return None, f"{path}: not JSON"
            if isinstance(data, dict) and data.get("Success") is False:
                err = str(data.get("Error") or "")
                # ninaAPI answers "no sequence" when nothing is loaded: idle
                if "no sequence" in err.lower() or "not loaded" in err.lower():
                    return [], None
                return None, f"{path}: {err}"
            return _state_payload(data), None
        return None, last
    finally:
        if own:
            await client.aclose()


async def safety_connected(base_url: str, client=None):
    """True / False from NINA's safety monitor info (read only), None when it
    cannot be read."""
    base = base_url.rstrip("/")
    own = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        r = await client.get(base + "/equipment/safetymonitor/info")
        if r.status_code >= 400:
            return None
        data = _state_payload(r.json())
        return bool((data or {}).get("Connected")) if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None
    finally:
        if own:
            await client.aclose()


async def load_only(base_url: str, seq: dict, client=None) -> dict:
    """POST the sequence to NINA's /sequence/load. Never stops or starts
    anything (rigs.nina_dispatch does stop + load + start). {ok, detail}."""
    base = base_url.rstrip("/")
    own = client is None
    client = client or httpx.AsyncClient(timeout=60)
    try:
        r = await client.post(base + "/sequence/load", json=seq)
        if r.status_code >= 400:
            return {"ok": False, "detail": f"HTTP {r.status_code}: {r.text[:300]}"}
        try:
            data = r.json()
        except ValueError:
            data = None
        if isinstance(data, dict) and data.get("Success") is False:
            return {"ok": False, "detail": str(data.get("Error"))}
        return {"ok": True, "detail": "loaded (not started)"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "detail": f"{type(e).__name__}: {e}"}
    finally:
        if own:
            await client.aclose()


# ------------------------------------------------------------- copy + record

def sideload_path(seq_dir: Path, rig: str, name: str,
                  now: datetime | None = None) -> Path:
    """<seq_dir>/Sideload_<rig>_<name>_<yyyymmdd_HHMMSS>.json"""
    now = now or datetime.now()
    safe = "".join(c if (c.isalnum() or c in "-_") else "_" for c in str(name))
    return Path(seq_dir) / f"Sideload_{rig}_{safe[:80]}_{now:%Y%m%d_%H%M%S}.json"


def record_event(config, rig: str, name: str, path: str, recipe: str | None,
                 ok: bool, detail: str, now: datetime | None = None) -> dict:
    """One line in runs/<night>_events.jsonl (kind "sideload") and one in the
    notification audit (sent=false), so the night keeps a record of what was
    loaded by hand."""
    from photonscript.shared.night_events import events_path
    from photonscript.shared.phd2_store import append_jsonl, iso_z, night_of
    now = now or datetime.utcnow()
    line = {"t": iso_z(now), "rig": rig, "src": "photonscript",
            "kind": "sideload", "value": name, "file": path,
            "recipe": recipe, "ok": bool(ok), "detail": detail}
    append_jsonl(events_path(config, night_of(config, now)), line)
    try:
        from photonscript.shared.pushover import record
        record(config, f"{'Loaded' if ok else 'Load FAILED'} on {rig}: {name} "
                       f"({detail}). Not started; Start stays in NINA.",
               title="PhotonScript sideload", reason="sideload")
    except Exception as e:  # noqa: BLE001
        logger.warning("sideload audit failed: %s", e)
    return line


def sequence_text(seq: dict) -> str:
    return json.dumps(seq, indent=2)
