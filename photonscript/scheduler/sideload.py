"""PS-123: sideload a hand-built night into NINA #1 / NINA #2 (load, never start).

On 2026-10-05 tonight's sequence (a tracking test on M 2, then tonight's
targets minus Cat's Eye) and the Piggy-600 companion were spliced on the
desktop and loaded by hand over ninaAPI. This module is that procedure as
pure functions, plus two tiny ninaAPI calls:

  splice_tracking_test()  tracking-test DeepSkyObjectContainer in the
                          Targets area BEFORE the night loop (PS-127: runs
                          once per night), then tonight's targets minus
                          `exclude` in tonight's own start / night loop /
                          shutdown; ids + Parent links rebuilt with
                          link_parents() (PS-77)
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
RECIPES = {
    RECIPE_TT_THEN_TONIGHT: (
        "RC16: tracking test on an auto-picked field, then tonight's targets "
        "(minus any excluded). Piggy-600: the companion with lights."),
}

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

# Instructions a camera-only companion must never carry: the Piggy-600 rides
# the RC16 mount, which NINA #1 owns.
MOUNT_TYPES = ("SlewScope", "ParkScope", "UnparkScope", "Platesolving.Center",
               "SetTracking", "MeridianFlip", "StartGuiding")


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
                         name: str | None = None) -> dict:
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

    Names in `exclude` match case- and whitespace-insensitively. Returns a
    new tree with fresh $id / Parent links; inputs are untouched. Raises
    SpliceError on an unexpected shape."""
    from photonscript.scheduler.nina_sequence_json import link_parents
    bad = set(non_parent_refs(night_seq)) | set(non_parent_refs(tt_seq))
    if bad:
        raise SpliceError(f"$ref outside Parent ({sorted(map(str, bad))}): "
                          "the splice would leave it dangling")
    night, tt = strip_ids(night_seq), strip_ids(tt_seq)
    tests = [v for v in _items(_targets_container(tt)) if _is_dso(v)]
    if len(tests) != 1:
        raise SpliceError(f"tracking-test sequence has {len(tests)} targets, "
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
    vals.insert(loop_at, _spliced_note(tests[0]))
    if name:
        night["Name"] = name
    return link_parents(night)


def _spliced_note(test: dict) -> dict:
    """The test container with the standalone park-and-hold sentence in its
    annotation replaced by SPLICED_TT_NOTE (the copy is already ours)."""
    from photonscript.scheduler.nina_sequence_json import TRACKING_TEST_PARK_NOTE
    for v in _items(test):
        text = v.get("Text") if isinstance(v, dict) else None
        if isinstance(text, str) and TRACKING_TEST_PARK_NOTE in text:
            v["Text"] = text.replace(TRACKING_TEST_PARK_NOTE, SPLICED_TT_NOTE)
    return test


def splice_name(date: str, field: str, kept: list[str]) -> str:
    """PhotonScript_<yyyymmdd>_TT_<field>_then_<target | tonight>."""
    def safe(s):
        return "_".join("".join(c if c.isalnum() else " " for c in str(s)).split())
    tail = safe(kept[0]) if len(kept) == 1 else ("tonight" if kept else "nothing")
    return f"PhotonScript_{date.replace('-', '')}_TT_{safe(field)}_then_{tail}"


# ------------------------------------------------------------------------ lint

def lint_companion(seq: dict):
    """Lint for the Piggy-600 companion (camera + focuser only). Errors: a
    warm or missing setpoint, missing $id / Parent links (PS-77), a light
    loop without its own Safety + Time condition, a stale focus seed, or any
    mount / guiding instruction. Returns a sequence_lint.LintResult."""
    from photonscript.scheduler import sequence_lint as sl
    r = sl.LintResult()
    cools = sl._find_type(seq, "CoolCamera")
    if not cools:
        r.warn("cooling", "No CoolCamera instruction found")
    for c in cools:
        temp = c.get("Temperature")
        if temp is None or temp > 0.0:
            r.error("cooling", f"CoolCamera Temperature is {temp!r}: must be at "
                               "or below 0.0 C (never a warm sensor)")
    sl._check_focus_moves(seq, r)
    sl._check_parent_links(seq, r)
    sl._check_light_loop_guards(seq, r)
    mount = sorted({_short(d["$type"]) for frag in MOUNT_TYPES
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
