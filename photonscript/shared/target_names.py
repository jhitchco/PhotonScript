"""Canonical target names (PS-78).

Since PS-27 the RC16 lights were named after NINA's innermost running
container ("Heart Nebula imaging (repeats while safe and up)_Container") and
the Piggy-600 lights after its OSC loop ("OSC_LIGHT_LOOP_Container"), because
NINA wrote no OBJECT and the telescope agent fell back to the live container
name. Goal sync, target history, the Library and the runs table all keyed on
that raw string, so the real targets showed nothing.

canonical_target() maps any of those names back to the target:

* strips ninaAPI's "_Container" and the generator's per-target container
  suffixes (" imaging (repeats while safe and up)", " focus calibration AFs",
  " unguided ladder"),
  repeatedly, case-insensitively;
* returns None ("unattributed") for structural loops that name no target
  (OSC_LIGHT_LOOP, SAFE_LOOP, "<filter> until moonrise", ...), so the PS-51
  header match / piggyback time correlation names those subs instead;
* matches the result case- and punctuation-insensitively against known
  targets (project names, catalog ids) and returns the known spelling;
* PS-135: a known target's catalog aliases match too (catalog_aliases:
  "NGC 224", "M31", "Messier 31", "Andromeda" all name the M 31 project).

The suffixes and loop names are imported from the sequence generator
(nina_sequence_json, calibration) and the NINA client, never duplicated, so a
container rename there keeps this mapping in step. Shared with PS-81 (Targets
tab), which reuses canonical_target() and target_key() at read time.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from functools import lru_cache
from typing import Any

UNATTRIBUTED = "?"


@lru_cache(maxsize=1)
def generator_names() -> dict:
    """Container naming constants, imported lazily from the generator (the
    scheduler modules import runs, which imports this module)."""
    from photonscript.scheduler import calibration as cal
    from photonscript.scheduler import nina_sequence_json as gen
    from photonscript.telescope_agent.nina_client import (
        NINAAPI_CONTAINER_SUFFIX,
        TARGETS_AREA_CONTAINER,
    )
    structural = (*gen.NIGHT_LOOP_CONTAINER_NAMES,
                  *cal.PIGGYBACK_LOOP_CONTAINER_NAMES,
                  TARGETS_AREA_CONTAINER[:-len(NINAAPI_CONTAINER_SUFFIX)])
    return {
        "container_suffix": NINAAPI_CONTAINER_SUFFIX,
        # suffixes that wrap a target name: strip them, keep the target
        "target_suffixes": (gen.TARGET_IMAGING_SUFFIX,
                            gen.TARGET_FOCUS_CAL_SUFFIX,
                            gen.TARGET_TRACKING_LADDER_SUFFIX,
                            gen.TARGET_BLOCK_SUFFIX),         # PS-61
        # suffixes that wrap a FILTER name: the container names no target
        "non_target_suffixes": (gen.FILTER_UNTIL_MOONRISE_SUFFIX,),
        "structural": frozenset(n.lower() for n in structural),
    }


def strip_container_name(name: Any) -> str:
    """The name with every container suffix removed ('' for blank / '?').
    Does not decide whether the result is a target (see canonical_target)."""
    s = str(name or "").strip()
    if not s or s == UNATTRIBUTED:
        return ""
    g = generator_names()
    suffixes = (g["container_suffix"], *g["target_suffixes"])
    changed = True
    while changed:
        changed = False
        low = s.lower()
        if low in g["structural"]:
            break  # "TARGETS_CONTAINER" is a loop name, not "TARGETS" + suffix
        for suf in suffixes:
            if suf and len(s) > len(suf) and low.endswith(suf.lower()):
                s = s[:-len(suf)].rstrip()
                changed = True
                break
    return s


def is_container_name(name: Any) -> bool:
    """True when the name is a generator/NINA container name rather than a
    plain target name (it canonicalizes to something else, or to None)."""
    s = str(name or "").strip()
    if not s or s == UNATTRIBUTED:
        return False
    return canonical_target(s) != s


def target_key(name: Any) -> str:
    """Comparison key: casefolded, curly quotes folded, only letters and
    digits ("Cat's Eye Nebula" == "cats eye nebula", "NGC 6543" ==
    "ngc6543"). '' for blank."""
    s = str(name or "").casefold().replace("’", "'")
    return re.sub(r"[^0-9a-z]+", "", s)


class TargetIndex(dict):
    """{target_key(alias): canonical name}; build once, pass many times."""


def catalog_aliases(name: Any) -> frozenset:
    """PS-135: every target_key the catalog knows for this object (catalog
    id, Messier / NGC / IC cross id, common name, CATALOG_EXTRAS aliases,
    user catalog); empty when the catalog does not know it. The one alias
    resolver: known_target_index() expands every known name through it."""
    try:
        from photonscript.shared.astronomy import catalog_alias_keys
        return catalog_alias_keys(name)
    except Exception:  # noqa: BLE001 - never let the catalog break naming
        return frozenset()


def known_target_index(known: Any) -> TargetIndex:
    """{target_key(alias): canonical name} from any of:
    a Mapping alias -> name; an iterable of names; or an iterable of project /
    target objects (ImagingProject.target, CelestialTarget: name and
    catalog_id). Earlier entries win on a key clash.

    PS-135: after the given names and ids, each target's catalog aliases
    (catalog_aliases) map to it too, so a sub named "NGC 224", "M31" or
    "Andromeda" resolves to the "Andromeda Galaxy" project. A given name or
    id always beats a catalog alias of another target."""
    if isinstance(known, TargetIndex):
        return known
    idx = TargetIndex()
    if not known:
        return idx
    pairs: list[tuple[Any, str]] = []  # (spelling, canonical name)

    def add(alias, name):
        k = target_key(alias)
        if k and name:
            pairs.append((alias, str(name)))
            if k not in idx:
                idx[k] = str(name)

    if isinstance(known, Mapping):
        for alias, name in known.items():
            add(name, name)
            add(alias, name)
    else:
        for item in known:
            if isinstance(item, str):
                add(item, item)
                continue
            tgt = getattr(item, "target", None) or item
            name = getattr(tgt, "name", None)
            if not name:
                continue
            add(name, name)
            cid = getattr(tgt, "catalog_id", None)
            if cid:
                add(cid, name)
    for alias, name in pairs:
        for k in catalog_aliases(alias):
            if k not in idx:
                idx[k] = name
    return idx


def canonical_target(name: Any, known_targets: Any = None) -> str | None:
    """The real target a sub-record / OBJECT / folder name refers to, or None
    when it names no target (blank, '?', or a structural loop such as the
    Piggy-600's OSC_LIGHT_LOOP_Container).

    known_targets (optional): project names, catalog ids, or project objects
    (see known_target_index). A match returns the known spelling; otherwise
    the stripped name comes back as-is."""
    s = strip_container_name(name)
    if not s:
        return None
    g = generator_names()
    low = s.lower()
    if low in g["structural"]:
        return None
    for suf in g["non_target_suffixes"]:
        if suf and low.endswith(suf.lower()):
            return None
    if known_targets:
        idx = known_target_index(known_targets)
        hit = idx.get(target_key(s))
        if hit:
            return hit
    return s


def canonical_or_unattributed(name: Any, known_targets: Any = None) -> str:
    """canonical_target() for storing in a sub record: '?' when None."""
    return canonical_target(name, known_targets) or UNATTRIBUTED


def same_target(a: Any, b: Any, known_targets: Any = None) -> bool:
    """True when both names canonicalize to the same (non-None) target."""
    ca = canonical_target(a, known_targets)
    cb = canonical_target(b, known_targets)
    return bool(ca and cb and target_key(ca) == target_key(cb))


def names_for(target: str, raw_names: Iterable[str],
              known_targets: Any = None) -> list[str]:
    """Every raw name in raw_names that refers to target (for folder / record
    lookups that must include the old container-named spellings)."""
    return [n for n in raw_names if same_target(n, target, known_targets)]
