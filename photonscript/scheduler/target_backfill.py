"""PS-78: one-time rename backfill for container-named subs.

Before PS-77, NINA wrote no OBJECT, so the telescope agent named RC16 subs
after the running container ("Heart Nebula imaging (repeats while safe and
up)_Container") and Piggy-600 subs after the OSC loop
("OSC_LIGHT_LOOP_Container"). The read side now canonicalizes those names
(shared/target_names); this command fixes the stored data:

1. Sub logs (data/runs/<date>_subs.jsonl): every container-derived target is
   rewritten to its canonical target, or to '?' for a structural loop, with
   the old value kept in `target_raw`. Unattributed Piggy-600 subs then go
   through the PS-51 time correlation (in memory, same rule as the dawn
   backfill), so they inherit the RC16 target they were riding with.
2. Library: links under a container-named top-level folder (in the Library
   root and in Library/_rejected) move to the real target folder, or to
   Library/_ when the sub is still unattributed. A rename, so Syncthing ships
   a move. A destination that already exists is a collision: reported and
   left in place, nothing is overwritten or deleted. Emptied container
   folders are removed (empty directories only).
3. Optional (--stamp-headers): rewrite a FITS OBJECT that still holds the
   container name (the agent used to stamp it) to the canonical target.
   Header only, never the pixel data; off by default.

Dry run unless apply=True. Goal progress is NOT synced here: the project
store belongs to the running server (a second process saving it would be
overwritten). After --apply, POST /api/projects2/recount (or let the next
approval / dawn backfill do it).
"""

from __future__ import annotations

import json
import logging
import os
from collections import Counter
from pathlib import Path

from photonscript.shared.target_names import (
    UNATTRIBUTED,
    canonical_target,
    is_container_name,
    known_target_index,
)

logger = logging.getLogger(__name__)

# Top-level Library folders that are never target folders
_NON_TARGET = {"calibration", "piggyback", "_rejected", "_"}


def _known_targets(config, dates) -> object:
    """Project names + catalog ids (read-only from projects.json: the
    ProjectStore constructor may save seeds) plus every night's plan names."""
    from photonscript.scheduler.runs import _plan_target_names
    names: dict = {}
    p = Path(config.data_dir) / "projects.json"
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
        for proj in (raw.values() if isinstance(raw, dict) else raw):
            t = (proj or {}).get("target") or {}
            n = str(t.get("name") or "").strip()
            if n:
                names[n] = n
                cid = str(t.get("catalog_id") or "").strip()
                if cid:
                    names[cid] = n
    except (OSError, ValueError, AttributeError) as e:
        logger.info("rename backfill: no project names (%s)", e)
    for d in dates:
        for n in _plan_target_names(config, d):
            names.setdefault(n, n)
    return known_target_index(names)


def _rewrite_records(subs: list[dict], known) -> Counter:
    """Canonicalize every sub's target in place. Returns
    Counter{(rig, raw, new): n} for the changed subs."""
    changed: Counter = Counter()
    for s in subs:
        raw = s.get("target")
        if raw in (None, "", UNATTRIBUTED):
            continue
        new = canonical_target(raw, known) or UNATTRIBUTED
        if new == raw:
            continue
        if not s.get("target_raw"):
            s["target_raw"] = raw
        s["target"] = new
        changed[(s.get("rig") or "rc16", raw, new)] += 1
    return changed


def _dirs_to_merge(root: Path, known, extra_skip=()) -> list[Path]:
    if not root.exists():
        return []
    skip = _NON_TARGET | {x.lower() for x in extra_skip}
    out = []
    for d in sorted(root.iterdir()):
        if d.is_dir() and d.name.lower() not in skip \
                and is_container_name(d.name):
            out.append(d)
    return out


def _same_file(a: Path, b: Path) -> bool:
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _remove_empty_dirs(top: Path) -> int:
    n = 0
    for d in sorted((p for p in top.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()
            n += 1
        except OSError:
            pass
    try:
        top.rmdir()
        n += 1
    except OSError:
        pass
    return n


def rename_backfill(config, apply: bool = False, dates=None,
                    stamp_headers: bool = False) -> dict:
    """Run (or dry-run) the PS-78 rename backfill. See the module doc."""
    from photonscript.scheduler.runs import (
        _safe_name,
        correlate_piggyback_records,
        edit_subs,
        library_root,
        runs_dir,
    )
    all_dates = sorted({f.name.split("_")[0]
                        for f in runs_dir(config).glob("*_subs.jsonl")})
    if dates:
        want = set(dates)
        all_dates = [d for d in all_dates if d in want]
    known = _known_targets(config, all_dates)

    # 1. sub logs
    nights = []
    per_target: Counter = Counter()      # new target -> subs re-attributed
    by_file: dict[str, str] = {}         # FITS basename -> final target
    ambiguous: set = set()
    header_jobs: list[tuple[str, str, str]] = []
    for d in all_dates:
        # PS-140: the rewrite holds the night lock from load to write
        with edit_subs(config, d, write=apply) as subs:
            changed = _rewrite_records(subs, known)
            n_corr, windows, _pending = correlate_piggyback_records(subs)
        still_unattributed = sum(
            1 for s in subs if s.get("rig") not in ("rc16", None, "")
            and s.get("target") in (None, "", UNATTRIBUTED)
            and s.get("target_raw"))
        for (_rig, _raw, new), n in changed.items():
            if new != UNATTRIBUTED:
                per_target[new] += n
        for name, n in windows.items():
            per_target[name] += n
        for s in subs:
            base = Path(str(s.get("abs_path") or s.get("file") or "")
                        .replace("\\", "/")).name
            if not base:
                continue
            t = s.get("target") or UNATTRIBUTED
            if base in by_file and by_file[base] != t:
                ambiguous.add(base)
            by_file[base] = t
            if stamp_headers and s.get("target_raw") \
                    and t != UNATTRIBUTED and s.get("abs_path"):
                header_jobs.append((s["abs_path"], s["target_raw"], t))
        if changed or n_corr:
            nights.append({
                "date": d,
                "renamed": [{"rig": rig, "from": raw, "to": new, "subs": n}
                            for (rig, raw, new), n in sorted(changed.items())],
                "piggyback_correlated": dict(windows),
                "piggyback_still_unattributed": still_unattributed,
            })
    for b in ambiguous:
        by_file.pop(b, None)

    # 2. Library folders
    lib = library_root(config)
    extra_skip = {str(getattr(config, "analysis_dropbox_subdir", "_analysis")
                      or "_analysis")}
    folders = []
    moves = collisions = 0
    for root in (lib, lib / "_rejected"):
        for src_dir in _dirs_to_merge(root, known, extra_skip):
            canon = canonical_target(src_dir.name, known)
            entry = {"folder": str(src_dir.relative_to(lib)),
                     "to": {}, "moved": 0, "collisions": []}
            for f in sorted(p for p in src_dir.rglob("*") if p.is_file()):
                rel = f.relative_to(src_dir)
                tgt = by_file.get(f.name)
                if tgt in (None, UNATTRIBUTED):
                    tgt = canon or UNATTRIBUTED
                dest_dir = root / _safe_name(tgt)
                dest = dest_dir / rel
                folder_name = str(dest_dir.relative_to(lib))
                if dest.exists():
                    collisions += 1
                    entry["collisions"].append({
                        "file": str(rel), "dest": str(dest.relative_to(lib)),
                        "same_file": _same_file(f, dest)})
                    continue
                entry["to"][folder_name] = entry["to"].get(folder_name, 0) + 1
                if apply:
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(f, dest)
                    except OSError as e:
                        entry["collisions"].append({
                            "file": str(rel), "dest": str(dest.relative_to(lib)),
                            "error": str(e)})
                        collisions += 1
                        continue
                entry["moved"] += 1
                moves += 1
            if apply:
                entry["empty_dirs_removed"] = _remove_empty_dirs(src_dir)
            folders.append(entry)

    # 3. optional OBJECT headers
    headers = {"requested": stamp_headers, "candidates": len(header_jobs),
               "stamped": 0}
    if stamp_headers and apply:
        from photonscript.shared.fits_object import header_object, stamp_object
        for path, raw, new in header_jobs:
            cur = header_object(path)
            if cur and cur == raw and stamp_object(path, new, overwrite=True):
                headers["stamped"] += 1

    res = {
        "applied": bool(apply),
        "nights_scanned": len(all_dates),
        "nights_changed": len(nights),
        "subs_renamed": sum(r["subs"] for n in nights for r in n["renamed"]),
        "subs_by_target": dict(sorted(per_target.items())),
        "piggyback_still_unattributed": sum(
            n["piggyback_still_unattributed"] for n in nights),
        "nights": nights,
        "library": str(lib),
        "library_folders": folders,
        "library_links_to_move": moves,
        "library_collisions": collisions,
        "headers": headers,
        "next": ("POST /api/projects2/recount to resync goal progress"
                 if apply else "dry run: nothing written; add --apply"),
    }
    logger.info("Rename backfill (%s): %d subs renamed over %d nights, "
                "%d library links %s, %d collisions",
                "applied" if apply else "dry run", res["subs_renamed"],
                res["nights_changed"], moves,
                "moved" if apply else "to move", collisions)
    return res
