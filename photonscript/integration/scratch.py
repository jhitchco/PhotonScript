"""Fast scratch: build integrate / blend run folders on a fast local disk,
keep the archive on the staging root.

The desktop staging root (D:/Astrophotography/Staging) is a USB spinning
drive (30 to 50 MB/s); a Piggy-600 M31 stack writes 250 to 350 GB of
PixInsight intermediates (out/<stack>/cal, cc, debayer, ln, reg) there.
With integration_scratch_root set (desktop .env PS_INTEGRATION_SCRATCH_ROOT,
e.g. C:/Astro/Scratch on the NVMe) a run is built in
<scratch>/<run name> instead and, once it is done, archived:

  * success or partial (at least one out/master/master_*.xisf): the keepers
    (KEEP_INTEGRATE: masters, finals, AstroBin packet, ledger, manifest,
    logs, timing, scripts, QA csv, reference.txt) are MOVED into
    <staging_root>/<run name>, their text paths rewritten from the scratch
    folder to the archive folder, then the scratch run folder is deleted
    (intermediates and the staged frame copies: copies of the read-only
    Library mirror, the manifest records each source so a rerun restages);
    integration_keep_staged_frames keeps LIGHTS / BIAS / DARKS / FLATS too;
  * failure without a master (or an error): the keepers are COPIED to the
    archive (the ledger moves, so there is one), the scratch folder stays
    for debugging, its size and the exact cleanup command are printed.

Preflight (before staging): free space on the scratch drive against the
staged frame bytes x 6 (OSC) or x 4 (mono) plus 10%; short = the run is
built on the staging root as before, with a warning (never a failure).

Deletes only ever remove one run folder strictly inside the scratch root,
never anything in a Library mirror or a ninashare path. Discovery
(ledgers, blend inputs, report sweep, watcher state) reads the staging
root first and the scratch root second (run_roots), so a run whose archive
step could not finish (D: unplugged) is still found.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

OSC_FACTOR = 6.0      # staged OSC frames -> cal + cc + debayer (3 ch) + reg + ln
MONO_FACTOR = 4.0     # staged mono frames -> cal + cc + reg + ln
MARGIN = 1.10

# relative globs (to the run folder) of what an integrate run keeps
KEEP_INTEGRATE = (
    "ledger.json", "manifest.json", "manifest.csv", "reference.txt", "review.jpg", "*.js",
    "qa/**/*", "astrobin/**/*", "out/master/**/*", "out/final*/**/*",
    "out/*.log", "out/*.csv", "out/*.json", "out/*.txt",
    "out/*/*.csv",                   # per stack weights.csv, dark_scaling.csv
)
STAGED_FRAMES = ("LIGHTS/**/*", "BIAS/**/*", "DARKS/**/*", "FLATS/**/*")
KEEP_BLEND = ("*", "out/*", "out/final/**/*")   # everything but out/work (registration work)
TEXT_SUFFIXES = (".json", ".csv", ".md", ".js", ".txt")
TEXT_MAX_BYTES = 20 * 1024 * 1024
LEDGER = "ledger.json"


def configured_root(cfg) -> Path | None:
    """integration_scratch_root as a Path, None when blank (off)."""
    s = str(getattr(cfg, "integration_scratch_root", "") or "").strip() if cfg is not None else ""
    return Path(s) if s else None


def _resolve(p: Path) -> Path:
    try:
        return Path(p).resolve()
    except OSError:
        return Path(os.path.abspath(p))


def _inside(path: Path, root: Path) -> bool:
    try:
        _resolve(path).relative_to(_resolve(root))
        return True
    except ValueError:
        return False


def usable(scratch_root: Path | None, staging_root: Path) -> bool:
    """A scratch root that is set and is not the staging root (or inside
    it, or holding it)."""
    if not scratch_root:
        return False
    a, b = _resolve(scratch_root), _resolve(staging_root)
    return not (a == b or _inside(a, b) or _inside(b, a))


def run_roots(staging_root: Path, scratch_root: Path | None = None) -> list[Path]:
    """Where run folders live: the staging (archive) root first, then the
    scratch root when one is in use."""
    roots = [Path(staging_root)]
    if usable(scratch_root, staging_root):
        roots.append(Path(scratch_root))
    return roots


def estimate_bytes(staged_bytes: int, osc: bool) -> int:
    return int(staged_bytes * (OSC_FACTOR if osc else MONO_FACTOR) * MARGIN)


def free_bytes(path: Path) -> int | None:
    """Free bytes on the drive of `path` (its nearest existing parent);
    None when it cannot be read."""
    p = Path(path)
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return int(shutil.disk_usage(p).free)
    except OSError:
        return None


def dir_size(path: Path) -> int:
    n = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                n += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return n


def gb(n: int | float | None) -> str:
    return "?" if n is None else f"{n / 1e9:.1f} GB"


def cleanup_command(work_dir: Path) -> str:
    return f'Remove-Item -LiteralPath "{_resolve(work_dir)}" -Recurse -Force'


def keep_files(run_dir: Path, patterns) -> list[Path]:
    """Files under run_dir matching the keep globs (relative paths, sorted)."""
    run_dir = Path(run_dir)
    out: set[Path] = set()
    for pat in patterns:
        for p in run_dir.glob(pat):
            if p.is_file():
                out.add(p.relative_to(run_dir))
    return sorted(out, key=lambda r: r.as_posix())


def keep_patterns(kind: str = "integrate", keep_staged: bool = False) -> tuple:
    """Keep globs. keep_staged: integrate = the staged frames too
    (integration_keep_staged_frames); blend = out/work too (--keep-work)."""
    if kind == "blend":
        return KEEP_BLEND + (("out/work/**/*",) if keep_staged else ())
    return KEEP_INTEGRATE + (STAGED_FRAMES if keep_staged else ())


def check_removable(work_dir: Path, scratch_root: Path, protect: list[Path] | None = None) -> Path:
    """The resolved run folder when it is safe to delete: strictly inside
    the scratch root, not inside any protected root (Library mirror,
    staging root) and no ninashare path. Raises ValueError otherwise."""
    w, r = _resolve(work_dir), _resolve(scratch_root)
    if w == r or not _inside(w, r):
        raise ValueError(f"refusing to delete {w}: not a run folder inside the scratch root {r}")
    if any(part.lower() == "ninashare" for part in w.parts):
        raise ValueError(f"refusing to delete {w}: a ninashare path")
    for p in protect or []:
        if p and (_inside(w, p) or _inside(p, w)):
            raise ValueError(f"refusing to delete {w}: overlaps {p}")
    return w


def _force_remove(func, path, _exc):
    """A read-only file (copied from the mirror): clear the flag, retry."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_run(work_dir: Path, scratch_root: Path, protect: list[Path] | None = None) -> None:
    w = check_removable(work_dir, scratch_root, protect)
    if sys.version_info >= (3, 12):
        shutil.rmtree(w, onexc=_force_remove)
    else:  # pragma: no cover - 3.11
        shutil.rmtree(w, onerror=_force_remove)


# --- path rewrite ---------------------------------------------------------------

def _variants(p: Path) -> list[str]:
    """JSON-escaped, native and forward-slash spellings of a folder."""
    s = str(p)
    return [s.replace("\\", "\\\\"), s, s.replace("\\", "/")]


def rewrite_text(text: str, old: Path, new: Path) -> str:
    for a, b in zip(_variants(old), _variants(new)):
        text = text.replace(a, b)
    return text


def rewrite_obj(obj, old: Path, new: Path):
    """Every string in a JSON-like object with the old folder replaced."""
    if isinstance(obj, str):
        return rewrite_text(obj, old, new)
    if isinstance(obj, list):
        return [rewrite_obj(x, old, new) for x in obj]
    if isinstance(obj, tuple):
        return tuple(rewrite_obj(x, old, new) for x in obj)
    if isinstance(obj, dict):
        return {k: rewrite_obj(v, old, new) for k, v in obj.items()}
    return obj


def _write_rewritten(src: Path, dst: Path, old: Path, new: Path, extra: dict | None = None) -> None:
    """dst = src with old -> new in its text (JSON reparsed, else plain)."""
    raw = src.read_bytes()
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".json":
        try:
            obj = rewrite_obj(json.loads(raw.decode("utf-8")), old, new)
            if extra and isinstance(obj, dict):
                obj.update(extra)
            tmp = dst.with_suffix(dst.suffix + ".tmp")
            tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=True), encoding="ascii")
            tmp.replace(dst)
            shutil.copystat(src, dst)
            return
        except ValueError:
            pass
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        shutil.copy2(src, dst)
        return
    dst.write_bytes(rewrite_text(text, old, new).encode("utf-8"))
    shutil.copystat(src, dst)


def _is_text(p: Path) -> bool:
    try:
        return p.suffix.lower() in TEXT_SUFFIXES and p.stat().st_size <= TEXT_MAX_BYTES
    except OSError:
        return False


# --- the workspace of one run ------------------------------------------------------

@dataclass
class Workspace:
    """One run built in the scratch root and archived to the staging root."""
    work_dir: Path                    # <scratch>/<run name> (or <scratch>/Blend/<name>)
    archive_dir: Path                 # <staging>/<run name>
    scratch_root: Path
    kind: str = "integrate"           # integrate | blend
    keep_staged: bool = False
    protect: list[Path] = field(default_factory=list)
    fell_back: bool = False           # preflight moved the run to the archive root
    notes: list[str] = field(default_factory=list)

    @property
    def run_dir(self) -> Path:
        return self.archive_dir if self.fell_back else self.work_dir

    @classmethod
    def create(cls, archive_dir: Path, scratch_root: Path, staging_root: Path, kind: str = "integrate",
               keep_staged: bool = False, protect: list[Path] | None = None) -> "Workspace":
        """The scratch twin of archive_dir. Raises ValueError when the
        archive folder is already taken (every run gets a NEW folder)."""
        archive_dir = Path(archive_dir)
        rel = _resolve(archive_dir).relative_to(_resolve(staging_root))
        work = Path(scratch_root) / rel
        for d in (archive_dir, work):
            if d.exists() and any(d.iterdir()):
                raise ValueError(f"{d} exists and is not empty: every run gets a NEW folder")
        return cls(work_dir=work, archive_dir=archive_dir, scratch_root=Path(scratch_root),
                   kind=kind, keep_staged=keep_staged,
                   protect=[Path(staging_root)] + [Path(p) for p in (protect or []) if p])

    # -- preflight --------------------------------------------------------------
    def preflight(self, staged_bytes: int, osc: bool, echo=print, free=None) -> Path:
        """Room on the scratch drive for this run? Short: move the (small)
        run folder to the archive root and go on there. Returns run_dir."""
        need = estimate_bytes(staged_bytes, osc)
        have = (free or free_bytes)(self.scratch_root)
        what = (f"needs about {gb(need)} ({gb(staged_bytes)} staged x "
                f"{OSC_FACTOR if osc else MONO_FACTOR:g} + 10%), {gb(have)} free on {self.scratch_root}")
        if have is None or have >= need:
            echo(f"  scratch: {what}: building in {self.work_dir}")
            self.notes.append(f"scratch {self.work_dir}: {what}")
            return self.run_dir
        echo(f"  WARNING: scratch too small: {what}; building on the staging root {self.archive_dir} "
             "instead (slower disk)")
        self.notes.append(f"scratch short ({what}): built on the staging root")
        self.archive_dir.parent.mkdir(parents=True, exist_ok=True)
        if self.archive_dir.exists():
            self.archive_dir.rmdir()          # empty (create() checked)
        if self.work_dir.exists():
            shutil.move(str(self.work_dir), str(self.archive_dir))
        else:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        self.fell_back = True
        return self.run_dir

    # -- archive ----------------------------------------------------------------
    def patterns(self) -> tuple:
        return keep_patterns(self.kind, self.keep_staged)

    def has_product(self) -> bool:
        """At least one master (integrate) or blend output (blend)."""
        if self.kind == "blend":
            return any(p.is_file() for p in (self.work_dir / "out" / "final").glob("*"))
        return any((self.work_dir / "out" / "master").glob("master_*.xisf"))

    def archive(self, move: bool, echo=print) -> dict:
        """Move (or copy) the keepers into the archive folder, text paths
        rewritten from the scratch folder to the archive folder. The ledger
        always moves, and last (one ledger per run, complete when it shows
        up in the archive). Returns {files, bytes, errors}."""
        src, dst = self.work_dir, self.archive_dir
        files = keep_files(src, self.patterns())
        led = [f for f in files if f.as_posix() == LEDGER]
        rest = [f for f in files if f.as_posix() != LEDGER]
        done, nbytes, errors = [], 0, []
        extra = {"scratch": {"work_dir": str(src), "archived_to": str(dst),
                             "staged_frames_kept": bool(self.keep_staged),
                             "note": ("staged frame copies were deleted with the scratch folder; "
                                      "frames[].source names each Library original")
                             if not self.keep_staged else ""}}
        for rel in rest + led:
            s, d = src / rel, dst / rel
            try:
                size = s.stat().st_size
                d.parent.mkdir(parents=True, exist_ok=True)
                if _is_text(s):
                    _write_rewritten(s, d, src, dst,
                                     extra if (self.kind == "integrate" and rel.as_posix() == "manifest.json")
                                     else None)
                    if move or rel.as_posix() == LEDGER:
                        s.unlink()
                elif move:
                    shutil.move(str(s), str(d))
                else:
                    shutil.copy2(s, d)
                done.append(rel.as_posix())
                nbytes += size
            except OSError as e:
                errors.append(f"{rel.as_posix()}: {e}")
                if move:
                    break                       # D: full or gone: stop, keep the rest in scratch
        return {"files": done, "bytes": nbytes, "errors": errors}

    def leftovers(self) -> list[Path]:
        """Files in the scratch folder that are not keepers (staged frames,
        PixInsight work): what a failed run keeps for debugging."""
        if not self.work_dir.exists():
            return []
        keep = set(keep_files(self.work_dir, self.patterns()))
        return [p for p in self.work_dir.rglob("*")
                if p.is_file() and p.relative_to(self.work_dir) not in keep]

    def finish(self, result: dict, echo=print, ok: bool | None = None) -> dict:
        """After the run: archive and clean up (module doc). `ok` overrides
        the product check (blend: PixInsight's own verdict)."""
        if self.fell_back or not self.work_dir.exists():
            return result
        product = self.has_product() if ok is None else (ok and self.has_product())
        echo(f"== archive {self.work_dir} -> {self.archive_dir}")
        info: dict = {"work_dir": str(self.work_dir), "archive_dir": str(self.archive_dir),
                      "product": product, "notes": list(self.notes)}
        a = self.archive(move=product, echo=echo)
        info.update(archived=len(a["files"]), archived_bytes=a["bytes"], errors=a["errors"])
        echo(f"  {'moved' if product else 'copied'} {len(a['files'])} file(s), {gb(a['bytes'])}")
        for e in a["errors"]:
            echo(f"  WARNING: not archived: {e}")
        if product and not a["errors"]:
            size = dir_size(self.work_dir)
            try:
                remove_run(self.work_dir, self.scratch_root, self.protect)
                info["removed_bytes"] = size
                echo(f"  scratch cleared: {gb(size)} of intermediates"
                     + ("" if self.keep_staged else " and staged frame copies") + " deleted")
            except (OSError, ValueError) as e:
                info["errors"].append(f"cleanup: {e}")
                echo(f"  WARNING: scratch not cleared ({e}); clear it with: "
                     f"{cleanup_command(self.work_dir)}")
        else:
            self._keep_message(info, echo, "no master was made" if not product
                               else "the archive step did not finish")
        if not a["errors"]:
            # every path the caller reads (run_dir, ledger, scripts, finals)
            # now lives in the archive (moved, or copied when kept)
            result = rewrite_obj(result, self.work_dir, self.archive_dir)
        result["scratch"] = info
        return result

    def after_error(self, echo=print) -> dict:
        """The run raised: copy the keepers to the archive, keep the
        scratch folder (or drop it when it held nothing else)."""
        if self.fell_back or not self.work_dir.exists():
            return {}
        info: dict = {"work_dir": str(self.work_dir), "archive_dir": str(self.archive_dir)}
        a = self.archive(move=False, echo=echo)
        info.update(archived=len(a["files"]), errors=a["errors"])
        if not a["errors"] and not self.leftovers():
            try:
                remove_run(self.work_dir, self.scratch_root, self.protect)
                info["removed"] = True
                return info
            except (OSError, ValueError):
                pass
        self._keep_message(info, echo, "the run failed")
        return info

    def _keep_message(self, info: dict, echo, why: str) -> None:
        size = dir_size(self.work_dir)
        info["kept_bytes"] = size
        info["cleanup"] = cleanup_command(self.work_dir)
        echo(f"  {why}: scratch folder KEPT for debugging, {gb(size)}: {self.work_dir}")
        echo(f"  records copied to {self.archive_dir}; when done, clear the scratch with:")
        echo(f"    {info['cleanup']}")
