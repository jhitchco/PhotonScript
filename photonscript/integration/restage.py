"""`photonscript restage`: rebuild an archived run's staged frame copies.

With fast scratch (scratch.py) a finished run keeps its records and masters
in <staging_root>/<run name> but its staged LIGHTS / BIAS / DARKS / FLATS
copies are deleted with the scratch folder. manifest.json frames[] keeps,
per staged frame, its Library original (source), its staged path and its
size (bytes). restage() copies every staged frame again from the Library
mirror (read only: files are only opened for reading) into a NEW or empty
folder under the same relative path (LIGHTS/<F>/<exp>, BIAS, DARKS/<e>s,
FLATS/<F>), checking each source against the manifest size and each copy
against its source, and copies the run's generated scripts
(integrate_run.js, hdr_run.js, finish_run.js), reference.txt and
manifest.json with their paths rewritten to the new folder, so the exact
same integration can run again. The ledger is NOT copied (one ledger per
run: the archived one stays the record). restage.json records what was done.

Refused: a folder that is not empty, inside the archived run or the Library
mirror, or a ninashare path; too little free space. A missing source or a
size mismatch is reported per frame (the rest is still copied) and fails the
command.
"""
from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path, PurePosixPath

from photonscript.integration import manifest as mf
from photonscript.integration import scratch as sc

STAGED_DIRS = ("LIGHTS", "BIAS", "DARKS", "FLATS")
SCRIPTS = ("integrate_run.js", "hdr_run.js", "finish_run.js")
COPY_TEXT = SCRIPTS + ("reference.txt", "manifest.json", "manifest.csv")
RECORD = "restage.json"
MARGIN = 1.02


class RestageError(ValueError):
    pass


@dataclass
class Item:
    rel: str            # posix path inside the run folder, e.g. LIGHTS/L/120s/x.fits
    source: Path
    bytes: int | None


@dataclass
class Plan:
    run_dir: Path
    out: Path
    items: list[Item] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    old_roots: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return sum(i.bytes or 0 for i in self.items)


def _norm(s: str) -> str:
    return str(s).replace("\\", "/").rstrip("/")


def staged_rel(staged: str, old_roots: list[str]) -> str | None:
    """The staged frame's path inside its run folder: relative to one of the
    run's old roots, else from its LIGHTS / BIAS / DARKS / FLATS part on.
    None when it has none (or would climb out)."""
    s = _norm(staged)
    rel = None
    for r in old_roots:
        r = _norm(r)
        if r and s.lower().startswith(r.lower() + "/"):
            rel = s[len(r) + 1:]
            break
    if rel is None:
        parts = s.split("/")
        idx = next((i for i, p in enumerate(parts) if p in STAGED_DIRS), None)
        if idx is None:
            return None
        rel = "/".join(parts[idx:])
    pp = PurePosixPath(rel)
    if not pp.parts or pp.parts[0] not in STAGED_DIRS or ".." in pp.parts or pp.is_absolute():
        return None
    return pp.as_posix()


def _remap(src: Path, old_lib: str | None, library: Path | None) -> Path:
    """A source under the manifest's Library, moved to `library`."""
    if not library or not old_lib:
        return src
    s, o = _norm(str(src)), _norm(old_lib)
    if s.lower().startswith(o.lower() + "/"):
        return Path(library) / s[len(o) + 1:]
    return src


def _ninashare(p: Path) -> bool:
    return any(x.lower() == "ninashare" for x in sc._resolve(p).parts)


def check_out(out: Path, run_dir: Path, library: Path | None) -> None:
    out = Path(out)
    if _ninashare(out):
        raise RestageError(f"refusing to write into a ninashare path: {out}")
    if library and (sc._inside(out, library) or sc._inside(library, out)):
        raise RestageError(f"refusing to write inside the Library mirror: {out}")
    if sc._inside(out, run_dir) or sc._inside(run_dir, out):
        raise RestageError(f"{out} overlaps the archived run {run_dir}: pick another --out")
    if out.exists() and (not out.is_dir() or any(out.iterdir())):
        raise RestageError(f"{out} exists and is not empty: restage into a NEW or empty folder")


def plan(run_dir: Path, out: Path, library: Path | None = None) -> Plan:
    run_dir = Path(run_dir)
    if not (run_dir / "manifest.json").is_file():
        raise RestageError(f"no manifest.json in {run_dir}: not an integrate run folder")
    try:
        man = mf.read(run_dir)
    except (OSError, ValueError) as e:
        raise RestageError(f"{run_dir / 'manifest.json'} unreadable: {e}") from None
    frames = man.get("frames") or []
    if not frames:
        raise RestageError(f"{run_dir / 'manifest.json'} lists no frames")
    old = [str(run_dir), str(sc._resolve(run_dir)), str(man.get("run_dir") or ""),
           str((man.get("scratch") or {}).get("work_dir") or ""),
           str((man.get("scratch") or {}).get("archived_to") or "")]
    p = Plan(run_dir=run_dir, out=Path(out), old_roots=[r for r in old if r])
    old_lib = str((man.get("options") or {}).get("library") or "") or None
    seen: set[str] = set()
    for e in frames:
        staged = str(e.get("staged") or "")
        if not staged:
            continue                      # QA left it out: never staged
        rel = staged_rel(staged, p.old_roots)
        if rel is None:
            p.errors.append(f"{e.get('file')}: staged path {staged} is not under "
                            "LIGHTS / BIAS / DARKS / FLATS")
            continue
        if rel.lower() in seen:
            continue
        seen.add(rel.lower())
        src = Path(str(e.get("source") or ""))
        if not str(e.get("source") or ""):
            p.errors.append(f"{rel}: no source in the manifest")
            continue
        p.items.append(Item(rel=rel, source=_remap(src, old_lib, library),
                            bytes=None if e.get("bytes") is None else int(e["bytes"])))
    return p


def _copy_text(src: Path, dst: Path, old_roots: list[str], new: Path, extra: dict | None) -> None:
    """dst = src with each old root -> new (JSON reparsed, else plain)."""
    text = src.read_text(encoding="utf-8", errors="replace")
    roots = sorted({r for r in old_roots if r}, key=len, reverse=True)
    if src.suffix.lower() == ".json":
        try:
            obj = json.loads(text)
            for r in roots:
                obj = sc.rewrite_obj(obj, Path(r), new)
            if extra and isinstance(obj, dict):
                obj.update(extra)
            dst.write_text(json.dumps(obj, indent=1, ensure_ascii=True), encoding="ascii")
            return
        except ValueError:
            pass
    for r in roots:
        text = sc.rewrite_text(text, Path(r), new)
    dst.write_text(text, encoding="utf-8")


def restage(p: Plan, echo=print, free=None, library: Path | None = None,
            now: datetime | None = None) -> dict:
    """Copy every planned frame (sources only read) and the run's scripts.
    Returns {out, copied, bytes, errors, scripts}."""
    check_out(p.out, p.run_dir, library)
    have = (free or sc.free_bytes)(p.out)
    need = int(p.total * MARGIN)
    if have is not None and have < need:
        raise RestageError(f"{p.out}: needs about {sc.gb(need)} for {len(p.items)} frames, "
                           f"{sc.gb(have)} free")
    p.out.mkdir(parents=True, exist_ok=True)
    errors = list(p.errors)
    copied, nbytes = 0, 0
    echo(f"== restage {len(p.items)} frames ({sc.gb(p.total)}) from the Library into {p.out}")
    for i, it in enumerate(p.items, 1):
        try:
            size = it.source.stat().st_size
        except OSError:
            errors.append(f"{it.rel}: source missing: {it.source}")
            continue
        if it.bytes is not None and size != it.bytes:
            errors.append(f"{it.rel}: source is {size} bytes, the manifest says {it.bytes}: "
                          f"{it.source}")
            continue
        dst = p.out / it.rel
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(it.source, dst)
            got = dst.stat().st_size
        except OSError as e:
            errors.append(f"{it.rel}: copy failed: {e}")
            continue
        if got != size:
            errors.append(f"{it.rel}: copy is {got} bytes, source {size}")
            continue
        copied += 1
        nbytes += size
        if i % 200 == 0:
            echo(f"  {i} / {len(p.items)}")
    scripts = []
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    for name in COPY_TEXT:
        src = p.run_dir / name
        if not src.is_file():
            continue
        extra = ({"restaged": {"from": str(p.run_dir), "at": stamp,
                               "note": "frames copied again from frames[].source"}}
                 if name == "manifest.json" else None)
        try:
            _copy_text(src, p.out / name, p.old_roots, p.out, extra)
            if name in SCRIPTS:
                scripts.append(str(p.out / name))
        except OSError as e:
            errors.append(f"{name}: not copied: {e}")
    res = {"from": str(p.run_dir), "out": str(p.out), "at": stamp, "frames": len(p.items),
           "copied": copied, "bytes": nbytes, "errors": errors, "scripts": scripts}
    try:
        (p.out / RECORD).write_text(json.dumps(res, indent=1, ensure_ascii=True), encoding="ascii")
    except OSError as e:
        errors.append(f"{RECORD}: {e}")
    echo(f"  copied {copied} of {len(p.items)} frames, {sc.gb(nbytes)}"
         + (f", {len(errors)} problem(s)" if errors else ", sizes verified"))
    for e in errors[:50]:
        echo(f"  ERROR: {e}")
    return res


def _pi_line(exe: str, script: str) -> str:
    return f'& "{exe}" -n --automation-mode "--run={script}" --force-exit'


def rerun_commands(res: dict, exe: str, run_name: str = "") -> list[str]:
    """What to run next: the run's own PixInsight scripts in the new folder
    (integrate, HDR, finish, in that order), then a re-finish line."""
    out = Path(res["out"])
    lines = [_pi_line(exe, str(out / s)) for s in SCRIPTS if (out / s).is_file()]
    name = (run_name or out.name).split("_restage_")[0]
    lines.append(f'photonscript finish --master "{out / "out" / "master" / "master_<F>.xisf"}" '
                 f'--name {name}_redo')
    return lines


def default_out(run_dir: Path, base: Path, now: datetime | None = None) -> Path:
    return Path(base) / f"{Path(run_dir).name}_restage_{(now or datetime.now()):%Y%m%d-%H%M}"
