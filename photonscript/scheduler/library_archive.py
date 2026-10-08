"""Library archive: move old lights/calibration OUT of the Syncthing share.

The Library (NINAShare\\Library) is what Syncthing ships to the desktop. Old
nights that are already integrated don't need to stay in the share: every file
there is part of Syncthing's "what does the desktop still need" walk, which is
what made /api/runs slow. This moves them to an archive folder OUTSIDE the
share (default: <share parent>\\NINAArchive\\Library\\..., same relative path),
so they keep existing on the scope PC but stop counting.

Consequences to know (by design, confirmed with Jeremy 2026-09-26):
  * The desktop mirrors deletions (receive-only), so desktop copies of moved
    files are removed too. The scope keeps two links to each frame: the NINA
    capture original and the archived link.
  * build_library() never re-links a night before the recorded cutoff (so a
    "Reset library" can't pull archived nights back into the share).

What moves:
  lights       every light whose night is before the cutoff. Night comes from
               the subs logs (file -> night), else the file's mtime.
  calibration  "flats" (default): FLAT sessions before the cutoff (flats are
               per-session). "all": also DARK/BIAS sessions before the cutoff.
               "none": no calibration. Darks/bias are reusable across months,
               so they stay unless asked; the plan lists them either way.
Never touched: the analysis dropbox (_analysis) and anything not *.fits.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CALIBRATION_MODES = ("flats", "all", "none")


def _state_path(config) -> Path:
    return Path(config.data_dir) / "library_archive.json"


def archive_cutoff(config) -> str:
    """Effective cutoff ('' = none): the later of the config value and the
    cutoff recorded by the last applied archive run."""
    cfg = (getattr(config, "library_archive_before", "") or "").strip()
    try:
        saved = json.loads(_state_path(config).read_text(encoding="utf-8"))
        rec = str(saved.get("before") or "")
    except Exception:  # noqa: BLE001 - no state yet
        rec = ""
    return max(cfg, rec)


def default_archive_root(config) -> Path:
    """<share parent>/NINAArchive: sibling of the folder holding Library."""
    from photonscript.scheduler.runs import library_root
    lib = library_root(config)
    explicit = (getattr(config, "library_archive_dir", "") or "").strip()
    if explicit:
        return Path(explicit)
    return lib.parent.parent / "NINAArchive"


def _night_index(config) -> dict[str, str]:
    """basename -> night date, from every subs log."""
    from photonscript.scheduler.runs import _load_subs, runs_dir
    idx: dict[str, str] = {}
    for f in runs_dir(config).glob("*_subs.jsonl"):
        date = f.name.split("_")[0]
        for s in _load_subs(config, date):
            for key in ("abs_path", "file"):
                v = s.get(key)
                if v:
                    idx.setdefault(Path(str(v).replace("\\", "/")).name, date)
    return idx


def _mtime_date(p: Path) -> str:
    try:
        return datetime.fromtimestamp(p.stat().st_mtime).strftime("%Y-%m-%d")
    except OSError:
        return ""


def plan_archive(config, before: str, calibration: str = "flats") -> dict:
    """What an archive run would move. Pure read; nothing changes."""
    from photonscript.scheduler.runs import library_root
    if not _DATE_RE.match(before or ""):
        raise ValueError("before must be YYYY-MM-DD")
    if calibration not in CALIBRATION_MODES:
        raise ValueError(f"calibration must be one of {CALIBRATION_MODES}")
    lib = library_root(config)
    dropbox = (getattr(config, "analysis_dropbox_subdir", "_analysis")
               or "_analysis").lower()
    idx = _night_index(config)
    moves: list[dict] = []
    kept_cal: dict[str, dict] = {}
    totals = {"lights": [0, 0], "FLAT": [0, 0], "DARK": [0, 0], "BIAS": [0, 0],
              "other_cal": [0, 0]}
    if not lib.exists():
        return {"library": str(lib), "before": before, "moves": [],
                "summary": {}, "kept_calibration": [], "note": "no library"}
    for f in lib.rglob("*.fits"):
        rel = f.relative_to(lib)
        parts = rel.parts
        if parts and parts[0].lower() == dropbox:
            continue
        try:
            size = f.stat().st_size
        except OSError:
            continue
        if "Calibration" in parts:
            i = parts.index("Calibration")
            typ = parts[i + 1].upper() if len(parts) > i + 1 else "CAL"
            date = next((p for p in parts[i + 2:] if _DATE_RE.match(p)), "") \
                or _mtime_date(f)
            if not date or date >= before:
                continue
            key = typ if typ in totals else "other_cal"
            wanted = (calibration == "all"
                      or (calibration == "flats" and typ == "FLAT"))
            if wanted:
                moves.append({"rel": str(rel), "kind": key, "date": date,
                              "bytes": size})
                totals[key][0] += 1
                totals[key][1] += size
            else:
                k = kept_cal.setdefault(f"{typ}/{date}",
                                        {"type": typ, "date": date, "files": 0})
                k["files"] += 1
            continue
        date = idx.get(f.name) or _mtime_date(f)
        if date and date < before:
            moves.append({"rel": str(rel), "kind": "lights", "date": date,
                          "bytes": size})
            totals["lights"][0] += 1
            totals["lights"][1] += size
    summary = {k: {"files": v[0], "gb": round(v[1] / 1e9, 2)}
               for k, v in totals.items() if v[0]}
    return {"library": str(lib), "archive_root": str(default_archive_root(config)),
            "before": before, "calibration": calibration,
            "files": len(moves),
            "gb": round(sum(m["bytes"] for m in moves) / 1e9, 2),
            "summary": summary,
            "kept_calibration": sorted(kept_cal.values(),
                                       key=lambda k: (k["type"], k["date"])),
            "moves": moves}


def run_archive(config, before: str, calibration: str = "flats",
                apply: bool = False, dest: str | None = None) -> dict:
    """Plan, and with apply=True move the files (same relative path under the
    archive root) and record the cutoff so build_library won't re-link them."""
    from photonscript.scheduler.runs import library_root
    plan = plan_archive(config, before, calibration)
    plan["applied"] = False
    if not apply:
        plan["moves"] = plan["moves"][:50]  # a sample is enough to eyeball
        return plan
    lib = library_root(config)
    root = Path(dest) if dest else default_archive_root(config)
    target_lib = root / lib.name
    share = lib.parent.resolve()
    t = target_lib.resolve()
    if t == share or share in t.parents:  # would still sync: refuse
        raise ValueError(f"archive {target_lib} is inside the shared folder "
                         f"{lib.parent}; pick a folder outside it")
    moved = failed = 0
    errors: list[str] = []
    for m in plan["moves"]:
        src = lib / m["rel"]
        dst = target_lib / m["rel"]
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists():
                src.unlink()  # already archived (a re-run): drop the share copy
            else:
                shutil.move(str(src), str(dst))
            moved += 1
        except OSError as e:
            failed += 1
            if len(errors) < 20:
                errors.append(f"{m['rel']}: {e}")
    _prune_empty_dirs(lib)
    state = {"before": max(before, archive_cutoff(config)),
             "calibration": calibration,
             "archive_root": str(root), "applied_at": datetime.now().isoformat(),
             "moved": moved, "failed": failed}
    p = _state_path(config)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(state, indent=1), encoding="utf-8")
    try:
        from photonscript.scheduler.runs import invalidate_fits_counts
        invalidate_fits_counts()
    except Exception:  # noqa: BLE001
        pass
    logger.info("Library archive before %s (%s): moved %d, failed %d -> %s",
                before, calibration, moved, failed, target_lib)
    plan.update(applied=True, moved=moved, failed=failed, errors=errors,
                archive_library=str(target_lib))
    plan["moves"] = plan["moves"][:50]
    return plan


def _prune_empty_dirs(root: Path) -> None:
    for d in sorted((p for p in root.rglob("*") if p.is_dir()),
                    key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()  # only succeeds when empty
        except OSError:
            pass


def archived_kinds(config, date: str) -> set[str]:
    """Which kinds of a night's files were archived (and must not be re-linked
    into the share): 'lights' plus the calibration types of the recorded mode.
    Empty set when the night is at/after the cutoff."""
    cut = archive_cutoff(config)
    if not cut or not date or date >= cut:
        return set()
    try:
        mode = json.loads(_state_path(config).read_text(encoding="utf-8")).get(
            "calibration", "flats")
    except Exception:  # noqa: BLE001 - cutoff from config only
        mode = "flats"
    kinds = {"lights"}
    if mode == "flats":
        kinds.add("FLAT")
    elif mode == "all":
        kinds |= {"FLAT", "DARK", "BIAS", "CAL"}
    return kinds


def night_is_archived(config, date: str) -> bool:
    return "lights" in archived_kinds(config, date)


__all__ = ["plan_archive", "run_archive", "archive_cutoff", "night_is_archived",
           "archived_kinds",
           "default_archive_root", "CALIBRATION_MODES"]

