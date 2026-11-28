"""PS-237 tilde rescue: plan getting the frames that exist only under
C:\\astro\\PhotonScript\\~\\Astrophotography back through the normal ingest.

Between 2026-09-26 and 10-09 the Librarian's legacy per-sub copy wrote 438
FITS into a literal "~" folder in the repo (an unexpanded
PS_LOCAL_IMAGE_DIR). A read-only survey on 2026-11-02 found 58 of them
(2.86 GB) nowhere else and listed them per file in
C:\\astro\\PhotonScript-tilde-check.csv.

DRY RUN BY DEFAULT: `plan()` only reads (the CSV, the FITS headers and a
name+size index of the Library and the nights' NINA folders) and says, per
frame, where it would go and what grades it. Nothing is copied, moved or
deleted.

The normal ingest is NINA's own folder: the RC16 night backfill
(runs.start_backfill, started when the night has ungraded frames) grades
every LIGHT under <image_watch_dir>/<night>/, then attribution, the pointing
pass and the Library build file it, graded, not blindly. So a rescued RC16
frame goes to <image_watch_dir>/<night>/<target>/LIGHT/<file>, as NINA
would have saved it. A TPoint point frame is graded into the night log but
never gets a Library folder (a calibration run, target_names). The
Piggy-600 is graded live only (no offline grader), so its frames are
planned but held.

`apply()` (CLI --apply) copies the planned RC16 frames, never overwriting,
never touching the source, and refuses while the service answers locally:
its file watcher would take a copied frame for a new capture tonight.
Deleting the "~" folder stays Jeremy's step.
"""

from __future__ import annotations

import csv
import logging
import shutil
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

from photonscript.shared.paths import safe_component, save_root_problem

logger = logging.getLogger(__name__)

DEFAULT_SOURCE = r"C:\astro\PhotonScript\~\Astrophotography"
DEFAULT_CSV = r"C:\astro\PhotonScript-tilde-check.csv"
FITS_EXTS = (".fits", ".fit", ".fts")
RC16_MIN_FOCAL_MM = 1500.0    # RC16 ~3290 mm; Piggy-600 600 mm
MONO_FILTERS = {"L", "R", "G", "B", "HA", "H", "OIII", "O", "SII", "S"}

_PATH_COLS = ("fullname", "full_path", "fullpath", "path", "relpath",
              "rel_path", "file", "filename", "name")
_STATUS_COLS = ("status", "verdict", "where", "found", "match", "matches",
                "copies", "elsewhere", "copy", "dup", "duplicate")
_UNIQUE_WORDS = ("unique", "nowhere", "none", "missing", "only")
_FALSE_WORDS = {"", "0", "no", "false", "n"}


@dataclass
class RescueItem:
    source: str
    name: str
    size: int | None = None
    rig: str = "?"
    night: str = ""
    target: str = ""
    filter: str = ""
    exp_s: float | None = None
    date_obs: str = ""
    dest: str = ""
    action: str = "hold"       # copy | hold | skip
    reason: str = ""
    notes: list[str] = field(default_factory=list)


def read_csv_rows(csv_path) -> tuple[list[dict], str]:
    """(rows, note). Rows are {"path", "size"} for the frames the survey
    marked unique. The column names are matched loosely (the survey CSV has
    no fixed schema here): a path column, an optional size column, and a
    "unique" flag or a status column whose value says unique / nowhere /
    none / 0. With no such column every row is returned and the note says
    so (the duplicate check in plan() still skips frames already present)."""
    text = Path(csv_path).read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(text.splitlines())
    cols = {c.strip().lower(): c for c in (reader.fieldnames or []) if c}
    path_col = next((cols[c] for c in _PATH_COLS if c in cols), None)
    if path_col is None:   # first column holding "path" / "file" / "name"
        path_col = next((orig for low, orig in cols.items()
                         if any(w in low for w in ("path", "file", "name"))),
                        None)
    if path_col is None:
        raise ValueError(f"{csv_path}: no path / file / name column "
                         f"(columns: {', '.join(cols.values()) or 'none'})")
    size_col = next((orig for low, orig in cols.items()
                     if any(w in low for w in ("size", "bytes", "length"))), None)
    uniq_col = next((orig for low, orig in cols.items() if "unique" in low), None)
    stat_col = None if uniq_col else next(
        (cols[c] for c in _STATUS_COLS if c in cols), None)

    rows, total = [], 0
    for r in reader:
        p = str(r.get(path_col) or "").strip()
        if not p:
            continue
        total += 1
        if uniq_col:
            v = str(r.get(uniq_col) or "").strip().lower()
            if v in _FALSE_WORDS:
                continue
        elif stat_col:
            v = str(r.get(stat_col) or "").strip().lower()
            if not (v in ("0", "no", "false", "")
                    or any(w in v for w in _UNIQUE_WORDS)):
                continue
        size = None
        if size_col:
            try:
                size = int(float(str(r.get(size_col) or "").strip()))
            except ValueError:
                size = None
        rows.append({"path": p, "size": size})
    if uniq_col:
        note = f"{len(rows)} of {total} rows marked unique ({uniq_col})"
    elif stat_col:
        note = f"{len(rows)} of {total} rows unique by {stat_col}"
    else:
        note = (f"no unique / status column: all {total} rows planned; "
                "frames already present are skipped")
    return rows, note


def resolve_source(path_text: str, source: Path) -> Path:
    """The frame's path under `source`: absolute paths as written (or, when
    they point at another copy of the "~" tree, re-rooted at the part after
    "~\\Astrophotography"), relative ones joined to `source`."""
    text = path_text.replace("/", "\\")
    low = text.lower()
    key = "~\\astrophotography\\"
    p = Path(path_text)
    if key in low and not p.exists():
        return source / Path(text[low.index(key) + len(key):].replace("\\", "/"))
    if p.is_absolute() or (len(text) > 2 and text[1] == ":"):
        return p
    return source / Path(text.replace("\\", "/"))


def _header(path: Path) -> dict:
    try:
        from astropy.io import fits
        h = fits.getheader(str(path))
        return {k: h.get(k) for k in ("OBJECT", "FILTER", "EXPTIME", "EXPOSURE",
                                      "DATE-OBS", "IMAGETYP", "FOCALLEN",
                                      "INSTRUME")}
    except Exception as e:  # noqa: BLE001 - unreadable: fall back to the tree
        return {"_error": str(e)}


def rig_of(hdr: dict, filter_name: str) -> str:
    """rc16 / piggyback from FOCALLEN, else from the filter (the Piggy-600
    is OSC: no wheel), else "?"."""
    try:
        fl = float(hdr.get("FOCALLEN") or 0)
    except (TypeError, ValueError):
        fl = 0.0
    if fl > 0:
        return "rc16" if fl >= RC16_MIN_FOCAL_MM else "piggyback"
    f = str(filter_name or "").strip().upper()
    if f in MONO_FILTERS:
        return "rc16"
    if f in ("OSC", "", "NONE", "?"):
        return "piggyback" if f == "OSC" else "?"
    return "?"


def _night_of(config, date_obs: str, fallback: float | None) -> str:
    from photonscript.shared.localtime import night_base_date
    when = None
    if date_obs:
        try:
            when = datetime.fromisoformat(str(date_obs).replace("Z", "")[:26])
        except ValueError:
            when = None
    if when is None and fallback is not None:
        when = datetime.utcfromtimestamp(fallback)
    if when is None:
        return ""
    return night_base_date(config, when).strftime("%Y-%m-%d")


def _tree_parts(path: Path, source: Path) -> tuple[str, str]:
    """(target, filter) from the Librarian layout <target>/<filter>/<file>;
    a "/" in the target (TPoint point 01/45 ...) nested it, so the filter
    is the parent folder and the target is everything above it."""
    try:
        rel = path.relative_to(source).parts
    except ValueError:
        return "", ""
    if len(rel) < 3:
        return (rel[0] if len(rel) == 2 else ""), ""
    return "/".join(rel[:-2]), rel[-2]


def _index(roots: list[Path]) -> dict[str, set]:
    """{file name (lower): {sizes}} of every FITS under the roots."""
    out: dict[str, set] = {}
    for r in roots:
        if not r or not r.exists():
            continue
        for f in r.rglob("*"):
            if f.suffix.lower() in FITS_EXTS:
                try:
                    out.setdefault(f.name.lower(), set()).add(f.stat().st_size)
                except OSError:
                    continue
    return out


def plan(config, source=DEFAULT_SOURCE, csv_path=DEFAULT_CSV,
         index_roots: list[Path] | None = None) -> dict:
    """Read-only plan (see the module doc). index_roots overrides the
    duplicate-check roots (tests)."""
    from photonscript.shared.rigs import PIGGYBACK, RC16, rig_config
    from photonscript.shared.target_names import is_calibration_run_target

    source = Path(source)
    rows, note = read_csv_rows(csv_path)
    items: list[RescueItem] = []
    for row in rows:
        p = resolve_source(row["path"], source)
        it = RescueItem(source=str(p), name=p.name, size=row.get("size"))
        if not p.is_file():
            it.action, it.reason = "skip", "source file not found"
            items.append(it)
            continue
        st = p.stat()
        if it.size is not None and it.size != st.st_size:
            it.notes.append(f"CSV size {it.size} != file size {st.st_size}")
        it.size = st.st_size
        hdr = _header(p)
        if "_error" in hdr:
            it.notes.append(f"header unreadable: {hdr['_error']}")
        t_tree, f_tree = _tree_parts(p, source)
        it.target = str(hdr.get("OBJECT") or t_tree or "").strip()
        it.filter = str(hdr.get("FILTER") or f_tree or "").strip()
        exp = hdr.get("EXPTIME") or hdr.get("EXPOSURE")
        it.exp_s = float(exp) if isinstance(exp, (int, float)) else None
        it.date_obs = str(hdr.get("DATE-OBS") or "")
        it.rig = rig_of(hdr, it.filter)
        it.night = _night_of(config, it.date_obs, st.st_mtime)
        imtype = str(hdr.get("IMAGETYP") or "LIGHT").upper()
        if "LIGHT" not in imtype:
            it.notes.append(f"IMAGETYP {imtype}: not a light")
        if it.rig == RC16:
            root = Path(config.image_watch_dir)
        elif it.rig == PIGGYBACK and getattr(config, "piggyback_image_watch_dir", ""):
            root = Path(rig_config(config, PIGGYBACK).image_watch_dir)
        else:
            root = None
        if root is not None and it.night:
            it.dest = str(root / it.night / safe_component(it.target)
                          / "LIGHT" / it.name)
        if is_calibration_run_target(it.target):
            it.notes.append("TPoint calibration run: graded into the night "
                            "log, never a Library folder")
        items.append(it)

    # duplicate check: same name and size in the Library or a night folder
    if index_roots is None:
        from photonscript.scheduler.runs import library_root
        nights = sorted({i.night for i in items if i.night})
        index_roots = [library_root(config)]
        for base in (getattr(config, "image_watch_dir", ""),
                     getattr(config, "piggyback_image_watch_dir", "")):
            if base:
                index_roots += [Path(base) / n for n in nights]
    seen = _index(index_roots)

    for it in items:
        if it.action == "skip":
            continue
        if it.size in seen.get(it.name.lower(), set()):
            it.action, it.reason = "skip", "same name and size already present"
        elif it.dest and Path(it.dest).exists():
            it.action, it.reason = "skip", "destination exists (different size)"
        elif it.rig == RC16 and it.dest:
            why = save_root_problem(str(Path(config.image_watch_dir)))
            if why:
                it.action, it.reason = "hold", f"PS_IMAGE_WATCH_DIR {why}"
            elif not any("not a light" in n for n in it.notes):
                it.action = "copy"
                it.reason = ("RC16 night backfill grades it (open the night "
                             "on the Runs page), then attribution, pointing "
                             "and the Library build")
            else:
                it.action, it.reason = "hold", "not a light"
        elif it.rig == PIGGYBACK:
            it.action, it.reason = "hold", ("Piggy-600 frames are graded live "
                                            "only: no offline grader to file "
                                            "them through")
        else:
            it.action, it.reason = "hold", "rig or night unknown"

    by = {a: [i for i in items if i.action == a] for a in ("copy", "hold", "skip")}
    return {
        "source": str(source), "csv": str(csv_path), "csv_note": note,
        "dry_run": True,
        "counts": {a: len(v) for a, v in by.items()},
        "bytes": {a: sum(i.size or 0 for i in v) for a, v in by.items()},
        "nights": sorted({i.night for i in by["copy"]}),
        "items": [asdict(i) for i in items],
    }


def service_answers(config, timeout: float = 2.0) -> bool:
    """True when the local scheduler answers /api/health (GET only)."""
    try:
        import httpx
        port = int(getattr(config, "scheduler_port", 8100) or 8100)
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=timeout)
        return r.status_code < 500
    except Exception:  # noqa: BLE001
        return False


def apply(config, result: dict) -> dict:
    """Copy the plan's "copy" frames (copy2, never overwrite, source kept).
    The caller has checked service_answers() is False."""
    done, failed = [], []
    for it in result.get("items", []):
        if it.get("action") != "copy":
            continue
        src, dest = Path(it["source"]), Path(it["dest"])
        try:
            if dest.exists():
                failed.append({"source": str(src), "error": "destination exists"})
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(str(src), str(dest))
            done.append(str(dest))
        except OSError as e:
            failed.append({"source": str(src), "error": str(e)})
    logger.info("tilde-rescue: %d copied, %d failed", len(done), len(failed))
    return {"copied": done, "failed": failed}
