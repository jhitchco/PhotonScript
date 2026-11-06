"""`photonscript scratch-status`: run folders left on the fast scratch disk.

A run built on integration_scratch_root (scratch.py) is deleted once its
keepers are archived; a run without a master, an error, or an archive step
that could not finish (D: unplugged) leaves its folder there, 50 to 350 GB
each. Older runs were built with --out under C:/Astro/Staging (the legacy
root) before fast scratch existed. This lists both:

    folder         <root>/<run> (and <root>/Blend/<run>, <root>/Finish/<run>)
    size, age      bytes on disk, days since the newest file changed
    product        a master (out/master/master_*.xisf) or a final (out/final*)
    archive twin   <integration_staging_root>/<same relative path> holds a
                   ledger.json or manifest.json (the keepers were archived)
    cleanup        the exact PowerShell Remove-Item line

clean(name) deletes ONE named folder inside those roots, through
scratch.check_removable (strictly inside the root, never a ninashare path,
never overlapping the staging root or the Library mirror), after the CLI has
shown its size and asked. Nothing outside the roots is ever touched.
"""
from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from photonscript.integration import scratch as sc

DEFAULT_SCRATCH = Path("C:/Astro/Scratch")
LEGACY_ROOT = Path("C:/Astro/Staging")
NESTED = ("Blend", "Finish")      # container folders holding run folders


@dataclass
class Folder:
    name: str                     # relative to its root (posix), e.g. "Blend/M31_blend_..."
    path: Path
    root: Path
    kind: str                     # scratch | legacy
    size: int
    age_days: float | None
    product: str                  # "master" | "final" | ""
    archive: Path | None          # the twin under the staging root, when it has records

    @property
    def cleanup(self) -> str:
        return sc.cleanup_command(self.path)

    def as_dict(self) -> dict:
        return {"name": self.name, "path": str(self.path), "root": str(self.root),
                "kind": self.kind, "bytes": self.size, "age_days": self.age_days,
                "product": self.product, "archive": str(self.archive) if self.archive else None,
                "cleanup": self.cleanup}


def _overlap(a: Path, b: Path) -> bool:
    ra, rb = sc._resolve(a), sc._resolve(b)
    return ra == rb or sc._inside(ra, rb) or sc._inside(rb, ra)


def roots(scratch_root: Path | None, staging_root: Path,
          legacy_root: Path | None = LEGACY_ROOT) -> list[tuple[Path, str]]:
    """[(root, kind)]: the scratch root and the legacy --out root, each only
    when it exists and does not overlap the staging (archive) root, the
    other root, or a ninashare path."""
    out: list[tuple[Path, str]] = []
    for r, kind in ((scratch_root, "scratch"), (legacy_root, "legacy")):
        if not r:
            continue
        r = Path(r)
        if any(p.lower() == "ninashare" for p in sc._resolve(r).parts):
            continue
        if _overlap(r, staging_root) or any(_overlap(r, o) for o, _k in out):
            continue
        if r.is_dir():
            out.append((r, kind))
    return out


def _walk(path: Path) -> tuple[int, float | None]:
    """(bytes, newest mtime) of every file under path."""
    n, newest = 0, None
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                st = os.stat(os.path.join(root, f))
            except OSError:
                continue
            n += st.st_size
            newest = st.st_mtime if newest is None else max(newest, st.st_mtime)
    if newest is None:
        try:
            newest = path.stat().st_mtime
        except OSError:
            pass
    return n, newest


def product_of(run: Path) -> str:
    if any((run / "out" / "master").glob("master_*.xisf")):
        return "master"
    for d in run.glob("out/final*"):
        if d.is_dir() and any(p.is_file() for p in d.rglob("*")):
            return "final"
    return ""


def archive_twin(rel: str, staging_root: Path) -> Path | None:
    twin = Path(staging_root) / rel
    if (twin / "ledger.json").is_file() or (twin / "manifest.json").is_file():
        return twin
    return None


def _run_dirs(root: Path):
    """(relative name, path) of every run folder under one root."""
    try:
        kids = sorted(p for p in root.iterdir() if p.is_dir())
    except OSError:
        return
    for p in kids:
        if p.name in NESTED:
            try:
                for q in sorted(x for x in p.iterdir() if x.is_dir()):
                    yield f"{p.name}/{q.name}", q
            except OSError:
                continue
        else:
            yield p.name, p


def scan(scratch_root: Path | None, staging_root: Path, legacy_root: Path | None = LEGACY_ROOT,
         now: float | None = None) -> list[Folder]:
    now = time.time() if now is None else now
    out = []
    for root, kind in roots(scratch_root, staging_root, legacy_root):
        for rel, path in _run_dirs(root):
            size, newest = _walk(path)
            age = None if newest is None else round(max(0.0, now - newest) / 86400.0, 1)
            out.append(Folder(name=rel, path=path, root=root, kind=kind, size=size,
                              age_days=age, product=product_of(path),
                              archive=archive_twin(rel, staging_root)))
    return out


def find(name: str, folders: list[Folder]) -> list[Folder]:
    """The listed folders called `name` (its relative name, or its last part)."""
    key = name.strip().replace("\\", "/").strip("/")
    hits = [f for f in folders if f.name == key]
    return hits or [f for f in folders if f.path.name == key]


def check(folder: Folder, staging_root: Path, protect: list[Path] | None = None) -> Path:
    """scratch.check_removable for a listed folder: strictly inside its root,
    a run folder (a child of the root or of root/Blend, root/Finish), no
    ninashare path, never overlapping the staging root or the Library."""
    w = sc.check_removable(folder.path, folder.root, [Path(staging_root)] + list(protect or []))
    parent = w.parent
    r = sc._resolve(folder.root)
    if parent != r and not (parent.parent == r and parent.name in NESTED):
        raise ValueError(f"refusing to delete {w}: not a run folder of {r}")
    return w


def clean(folder: Folder, staging_root: Path, protect: list[Path] | None = None) -> int:
    """Delete one listed run folder (after check()). Returns the bytes freed."""
    check(folder, staging_root, protect)
    size = folder.size
    sc.remove_run(folder.path, folder.root, [Path(staging_root)] + list(protect or []))
    return size


def age_text(d: float | None) -> str:
    if d is None:
        return "?"
    return f"{d * 24:.0f} h" if d < 1 else f"{d:.0f} d"


def format_rows(folders: list[Folder]) -> list[str]:
    lines = []
    for f in folders:
        twin = f"archived: {f.archive}" if f.archive else "NOT archived"
        prod = {"master": "master", "final": "final"}.get(f.product, "no master")
        lines.append(f"{f.kind:7} {f.name}  {sc.gb(f.size)}, {age_text(f.age_days)} old, "
                     f"{prod}, {twin}")
        lines.append(f"        {f.cleanup}")
    return lines
